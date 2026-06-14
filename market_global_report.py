#!/usr/bin/env python3
"""
market_global_report.py v1.0.0 — Global Crypto Market Snapshot

Fetches live market data from multiple sources and outputs a structured
global market report as JSON (and optionally Markdown). Not focused on
any specific token — market-wide by definition.

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
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from dateutil import parser as dateutil_parser
from dotenv import load_dotenv
from rss_utils import fetch_rss_feeds, score_sentiment, CRYPTO_RSS_FEEDS

SCRIPT_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0.0"
API_TIMEOUT = 10  # seconds

CMC_BASE  = "https://pro-api.coinmarketcap.com"
CG_BASE   = "https://api.coingecko.com/api/v3"
FRED_BASE = "https://api.stlouisfed.org/fred"

STABLECOIN_SYMBOLS = {
    "USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP", "USDD",
    "FRAX", "LUSD", "GUSD", "WBTC", "STETH", "CBETH", "RETH", "WETH",
}


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
            # retry once
            try:
                result = fetch_fn()
                print(f"  \u2713 {label} (retry)")
                return result
            except Exception as e2:
                msg2 = f"{label} — retry failed: {str(e2)}"
                print(f"  \u2717 {msg2}")
                if fetch_errors is not None:
                    fetch_errors.append(msg2)
                return None
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
# Header helpers
# ---------------------------------------------------------------------------

def _cg_headers(api_key: str) -> dict:
    return {"x-cg-demo-api-key": api_key, "Accept": "application/json"}


def _cmc_headers(api_key: str) -> dict:
    return {"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"}


# ---------------------------------------------------------------------------
# Signal lean helpers
# ---------------------------------------------------------------------------

def lean_market_cap_direction(change_24h_pct: Optional[float]) -> Optional[str]:
    if change_24h_pct is None:
        return None
    if change_24h_pct > 2.0:
        return "bullish"
    if change_24h_pct < -2.0:
        return "bearish"
    return "neutral"


def lean_btc_dominance(pct: Optional[float]) -> Optional[str]:
    if pct is None:
        return None
    if pct < 52.0:
        return "bullish"
    if pct > 62.0:
        return "bearish"
    return "neutral"


def lean_altcoin_season(index: Optional[int]) -> Optional[str]:
    if index is None:
        return None
    if index >= 75:
        return "bullish"
    if index <= 25:
        return "bearish"
    return "neutral"


def lean_btc_direction(change_24h: Optional[float]) -> Optional[str]:
    if change_24h is None:
        return None
    if change_24h > 1.0:
        return "bullish"
    if change_24h < -1.0:
        return "bearish"
    return "neutral"


def lean_funding_rate(avg: Optional[float]) -> Optional[str]:
    if avg is None:
        return None
    if avg < -0.0003:
        return "bullish"
    if avg > 0.0005:
        return "bearish"
    return "neutral"


def lean_btc_ls(ratio: Optional[float]) -> Optional[str]:
    if ratio is None:
        return None
    if ratio < 0.9:
        return "bullish"
    if ratio > 1.5:
        return "bearish"
    return "neutral"


def lean_eth_btc(eth_7d: Optional[float], btc_7d: Optional[float]) -> Optional[str]:
    if eth_7d is None or btc_7d is None:
        return None
    diff = eth_7d - btc_7d
    if diff > 2.0:
        return "bullish"
    if diff < -2.0:
        return "bearish"
    return "neutral"


def lean_dxy(latest: Optional[float], prior: Optional[float]) -> Optional[str]:
    if latest is None or prior is None or prior == 0:
        return None
    change_pct = ((latest - prior) / prior) * 100
    if change_pct > 0.3:
        return "bearish"
    if change_pct < -0.3:
        return "bullish"
    return "neutral"


def lean_spy(change_pct: Optional[float]) -> Optional[str]:
    if change_pct is None:
        return None
    if change_pct > 0.5:
        return "bullish"
    if change_pct < -0.5:
        return "bearish"
    return "neutral"


def lean_vix(vix: Optional[float], prior: Optional[float]) -> Optional[str]:
    if vix is None:
        return None
    if vix < 20 and prior is not None and vix < prior:
        return "bullish"
    if vix > 25:
        return "bearish"
    if prior is not None and prior != 0:
        pct_change = ((vix - prior) / prior) * 100
        if pct_change > 10:
            return "bearish"
    return "neutral"


def lean_fear_greed(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    if value < 25:
        return "bullish"
    if value > 75:
        return "bearish"
    return "neutral"


def lean_news_volume(ratio: Optional[float]) -> str:
    if ratio is None:
        return "neutral"
    if ratio > 0.65:
        return "bullish"
    if ratio < 0.35:
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
# GitHub push
# ---------------------------------------------------------------------------

def push_report_to_github(filepath):
    """Push a report file to the Muneo GitHub repo. Non-blocking."""
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    branch = os.getenv("GITHUB_BRANCH", "main")

    if not token or not repo:
        print("  \u26a0 GitHub push skipped — GITHUB_TOKEN or GITHUB_REPO not set in .env")
        return

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        filename = os.path.basename(filepath)
        github_path = f"reports/{filename}"
        api_url = f"https://api.github.com/repos/{repo}/contents/{github_path}"
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        encoded = base64.b64encode(content.encode()).decode()

        sha = None
        check = requests.get(api_url, headers=headers, timeout=10)
        if check.status_code == 200:
            sha = check.json().get("sha")

        payload = {
            "message": f"Add report: {filename}",
            "content": encoded,
            "branch": branch
        }
        if sha:
            payload["sha"] = sha

        response = requests.put(api_url, json=payload, headers=headers, timeout=15)

        if response.status_code in (200, 201):
            print(f"  \u2713 GitHub push — {github_path}")
        else:
            print(f"  \u2717 GitHub push failed — HTTP {response.status_code}: {response.text[:100]}")

    except Exception as e:
        print(f"  \u2717 GitHub push failed — {str(e)}")


# ---------------------------------------------------------------------------
# Markdown helpers
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
    ms      = report["market_structure"]
    btc     = report["btc"]
    eth     = report["eth"]
    defi    = report["defi"]
    macro   = report["macro"]
    sent    = report["sentiment"]
    news    = report["news"]
    sig     = report["signal_summary"]
    errors  = report.get("fetch_errors", [])

    lines = []

    # Header
    lines.append(f"# Muneo Global Market Report — {meta['generated_at'][:10]}")
    lines.append(f"**Generated:** {meta['generated_at']}  ")
    lines.append(f"**Script version:** {meta['script_version']}  ")
    lines.append(f"**Report type:** {meta['report_type']}")
    lines.append("")

    # Market Structure
    lines.append("## Market Structure")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Total Market Cap | {_usd(ms.get('total_market_cap_usd'))} |")
    lines.append(f"| Market Cap 24h Change | {_pct(ms.get('total_market_cap_change_24h_pct'))} |")
    lines.append(f"| Total Volume 24h | {_usd(ms.get('total_volume_24h_usd'))} |")
    lines.append(f"| BTC Dominance | {_fmt(ms.get('btc_dominance_pct'), 2)}% |")
    lines.append(f"| ETH Dominance | {_fmt(ms.get('eth_dominance_pct'), 2)}% |")
    lines.append(f"| Total2 (ex-BTC) | {_usd(ms.get('total2_usd'))} |")
    lines.append(f"| Total3 (ex-BTC, ex-ETH) | {_usd(ms.get('total3_usd'))} |")
    lines.append(f"| Active Cryptocurrencies | {ms.get('active_cryptocurrencies', 'N/A')} |")
    lines.append(f"| Altcoin Season Index | {ms.get('altcoin_season_index', 'N/A')} |")
    lines.append(f"| Altcoin Season Label | {ms.get('altcoin_season_label', 'N/A')} |")
    alt_lean = ms.get('altcoin_season_lean')
    lines.append(f"| Altcoin Season Lean | {_lean_icon(alt_lean)} {alt_lean or 'N/A'} |")
    lines.append("")

    lines.append("### Top Gainers (24h)")
    gainers = ms.get("top_gainers", [])
    if gainers:
        lines.append("| Symbol | Name | 24h Change | Price | Market Cap |")
        lines.append("|---|---|---|---|---|")
        for g in gainers:
            lines.append(
                f"| {g.get('symbol')} | {g.get('name')} | "
                f"{_pct(g.get('price_change_24h_pct'))} | "
                f"{_usd(g.get('price_usd'))} | "
                f"{_usd(g.get('market_cap_usd'))} |"
            )
    else:
        lines.append("*No data available.*")
    lines.append("")

    lines.append("### Top Losers (24h)")
    losers = ms.get("top_losers", [])
    if losers:
        lines.append("| Symbol | Name | 24h Change | Price | Market Cap |")
        lines.append("|---|---|---|---|---|")
        for lo in losers:
            lines.append(
                f"| {lo.get('symbol')} | {lo.get('name')} | "
                f"{_pct(lo.get('price_change_24h_pct'))} | "
                f"{_usd(lo.get('price_usd'))} | "
                f"{_usd(lo.get('market_cap_usd'))} |"
            )
    else:
        lines.append("*No data available.*")
    lines.append("")

    # BTC
    lines.append("## BTC")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Price (USD) | {_usd(btc.get('price_usd'))} |")
    lines.append(f"| 24h Change | {_pct(btc.get('price_change_24h_pct'))} |")
    lines.append(f"| 7d Change | {_pct(btc.get('price_change_7d_pct'))} |")
    lines.append(f"| Market Cap | {_usd(btc.get('market_cap_usd'))} |")
    lines.append(f"| Volume 24h | {_usd(btc.get('volume_24h_usd'))} |")
    lines.append(f"| Direction | {btc.get('direction', 'N/A')} |")
    lines.append(f"| Open Interest | {_usd(btc.get('oi_usd'))} |")
    lines.append(f"| Funding Rate (latest) | {_fmt(btc.get('funding_rate_latest'), 6)} |")
    lines.append(f"| Funding Rate (7d avg) | {_fmt(btc.get('funding_rate_7d_avg'), 6)} |")
    fund_lean = btc.get('funding_lean')
    lines.append(f"| Funding Lean | {_lean_icon(fund_lean)} {fund_lean or 'N/A'} |")
    lines.append(f"| Long/Short Ratio | {_fmt(btc.get('long_short_ratio'), 4)} |")
    lines.append(f"| Long Account % | {_fmt(btc.get('long_account_pct'), 2)} |")
    lines.append(f"| Short Account % | {_fmt(btc.get('short_account_pct'), 2)} |")
    ls_lean = btc.get('btc_ls_lean')
    lines.append(f"| L/S Lean | {_lean_icon(ls_lean)} {ls_lean or 'N/A'} |")
    lines.append("")

    # ETH
    lines.append("## ETH")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Price (USD) | {_usd(eth.get('price_usd'))} |")
    lines.append(f"| 24h Change | {_pct(eth.get('price_change_24h_pct'))} |")
    lines.append(f"| 7d Change | {_pct(eth.get('price_change_7d_pct'))} |")
    lines.append(f"| Market Cap | {_usd(eth.get('market_cap_usd'))} |")
    lines.append(f"| Volume 24h | {_usd(eth.get('volume_24h_usd'))} |")
    lines.append(f"| ETH/BTC Ratio | {_fmt(eth.get('eth_btc_ratio'), 6)} |")
    ethbtc_lean = eth.get('eth_btc_lean')
    lines.append(f"| ETH/BTC Lean | {_lean_icon(ethbtc_lean)} {ethbtc_lean or 'N/A'} |")
    lines.append("")

    # DeFi
    lines.append("## DeFi")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Total DeFi TVL | {_usd(defi.get('total_defi_tvl_usd'))} |")
    lines.append(f"| Stablecoin Market Cap | {_usd(defi.get('stablecoin_market_cap_usd'))} |")
    lines.append(f"| Stablecoin Dominance | {_pct(defi.get('stablecoin_dominance_pct'))} |")
    lines.append("")

    # Macro
    lines.append("## Macro")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
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
    lines.append("")

    # News
    lines.append("## News")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Source | {news.get('source', 'N/A')} |")
    lines.append(f"| Articles (24h) | {news.get('article_count_24h', 'N/A')} |")
    lines.append(f"| Positive | {news.get('positive_count', 'N/A')} |")
    lines.append(f"| Negative | {news.get('negative_count', 'N/A')} |")
    lines.append(f"| Neutral | {news.get('neutral_count', 'N/A')} |")
    lines.append(f"| Sentiment Ratio | {_fmt(news.get('sentiment_ratio'), 4)} |")
    nv_lean = news.get('news_volume_lean')
    lines.append(f"| News Volume Lean | {_lean_icon(nv_lean)} {nv_lean or 'null'} |")
    spike_val = news.get('news_spike')
    lines.append(f"| Spike (>20 articles) | {'Yes' if spike_val else 'No'} |")
    lines.append("")

    headlines = news.get("top_headlines", [])
    if headlines:
        lines.append("### Top Headlines")
        for h in headlines:
            sent = h.get("sentiment", {})
            polarity = sent.get("polarity", "neutral")
            score = sent.get("score", 0.0)
            lines.append(
                f"- **[{h.get('title', 'No title')}]({h.get('url', '#')})** "
                f"— {h.get('source', 'Unknown')} "
                f"({h.get('published_at', '')[:10]}) "
                f"| sentiment: {polarity} ({score:+.2f})"
                + (" \u26a0\ufe0f BREAKING" if h.get("is_breaking") else "")
            )
        lines.append("")

    # Signal Summary
    lines.append("## Signal Summary")
    overall = sig.get('overall_lean', 'N/A')
    lines.append(f"**Overall Lean:** `{overall}`")
    lines.append(
        f"Signals evaluated: {sig.get('signals_evaluated', 0)} | "
        f"Available: {sig.get('signals_available', 0)} | "
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
    cg_api_key: str,
    cmc_api_key: str,
    fred_api_key: str,
    alpha_vantage_api_key: str,
) -> dict:
    fetch_errors: list = []
    now_utc = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # [1/17] CoinGecko — global market data
    # ------------------------------------------------------------------
    print("[1/17] Fetching CoinGecko — global market data...")

    total_market_cap_usd        = None
    total_volume_24h_usd        = None
    btc_dominance_pct           = None
    eth_dominance_pct           = None
    total_market_cap_change_24h = None
    active_cryptocurrencies     = None
    total2_usd                  = None
    total3_usd                  = None

    cg_global = safe_fetch(
        "CoinGecko global market data",
        lambda: _fetch_cg_global(cg_api_key),
        fetch_errors,
    )
    time.sleep(2)

    if cg_global:
        try:
            data = cg_global.get("data", {})
            total_market_cap_usd        = data.get("total_market_cap", {}).get("usd")
            total_volume_24h_usd        = data.get("total_volume", {}).get("usd")
            btc_dominance_pct           = data.get("market_cap_percentage", {}).get("btc")
            eth_dominance_pct           = data.get("market_cap_percentage", {}).get("eth")
            total_market_cap_change_24h = data.get("market_cap_change_percentage_24h_usd")
            active_cryptocurrencies     = data.get("active_cryptocurrencies")
            if total_market_cap_usd and btc_dominance_pct is not None:
                total2_usd = total_market_cap_usd - (btc_dominance_pct / 100 * total_market_cap_usd)
            if total2_usd and eth_dominance_pct is not None and total_market_cap_usd:
                total3_usd = total2_usd - (eth_dominance_pct / 100 * total_market_cap_usd)
        except Exception as e:
            fetch_errors.append(f"cg_global:parse_error: {e}")

    # ------------------------------------------------------------------
    # [2/17] CoinGecko — top 100 coins (for altcoin season index)
    # ------------------------------------------------------------------
    print("[2/17] Fetching CoinGecko — top 100 coins by market cap...")

    altcoin_season_index = None
    altcoin_season_label = None

    cg_top100 = safe_fetch(
        "CoinGecko top 100 coins",
        lambda: _fetch_cg_top100(cg_api_key),
        fetch_errors,
    )
    time.sleep(2)

    if cg_top100:
        try:
            btc_entry = next((c for c in cg_top100 if c.get("id") == "bitcoin"), None)
            btc_90d = btc_entry.get("price_change_percentage_7d_in_currency") if btc_entry else None

            if btc_90d is not None:
                filtered = [
                    c for c in cg_top100
                    if c.get("id") != "bitcoin"
                    and c.get("symbol", "").upper() not in STABLECOIN_SYMBOLS
                ]
                alts_outperforming = sum(
                    1 for c in filtered
                    if c.get("price_change_percentage_7d_in_currency") is not None
                    and c["price_change_percentage_7d_in_currency"] > btc_90d
                )
                if filtered:
                    altcoin_season_index = round((alts_outperforming / len(filtered)) * 100)
                    if altcoin_season_index >= 75:
                        altcoin_season_label = "altcoin_season"
                    elif altcoin_season_index <= 25:
                        altcoin_season_label = "bitcoin_season"
                    else:
                        altcoin_season_label = "neutral"
        except Exception as e:
            fetch_errors.append(f"altcoin_season:compute_error: {e}")

    altcoin_season_lean = lean_altcoin_season(altcoin_season_index)

    # ------------------------------------------------------------------
    # [3/17] CoinGecko — top gainers and losers
    # ------------------------------------------------------------------
    print("[3/17] Fetching CoinGecko — top gainers and losers...")

    top_gainers: list = []
    top_losers: list  = []

    cg_markets = safe_fetch(
        "CoinGecko top gainers and losers",
        lambda: _fetch_cg_markets_250(cg_api_key),
        fetch_errors,
    )
    time.sleep(2)

    if cg_markets:
        try:
            filtered_markets = [
                c for c in cg_markets
                if c.get("market_cap") and c["market_cap"] > 50_000_000
                and c.get("price_change_percentage_24h_in_currency") is not None
            ]
            sorted_desc = sorted(
                filtered_markets,
                key=lambda c: c["price_change_percentage_24h_in_currency"],
                reverse=True,
            )
            for c in sorted_desc[:5]:
                top_gainers.append({
                    "symbol": c.get("symbol", "").upper(),
                    "name": c.get("name"),
                    "price_change_24h_pct": round(c["price_change_percentage_24h_in_currency"], 4),
                    "price_usd": c.get("current_price"),
                    "market_cap_usd": c.get("market_cap"),
                })
            sorted_asc = sorted(
                filtered_markets,
                key=lambda c: c["price_change_percentage_24h_in_currency"],
            )
            for c in sorted_asc[:5]:
                top_losers.append({
                    "symbol": c.get("symbol", "").upper(),
                    "name": c.get("name"),
                    "price_change_24h_pct": round(c["price_change_percentage_24h_in_currency"], 4),
                    "price_usd": c.get("current_price"),
                    "market_cap_usd": c.get("market_cap"),
                })
        except Exception as e:
            fetch_errors.append(f"gainers_losers:parse_error: {e}")

    # ------------------------------------------------------------------
    # [4/17] CoinGecko — BTC market data
    # ------------------------------------------------------------------
    print("[4/17] Fetching CoinGecko — BTC market data...")

    btc_price_usd      = None
    btc_change_24h     = None
    btc_change_7d      = None
    btc_market_cap_usd = None
    btc_volume_24h     = None
    btc_direction      = None

    cg_btc = safe_fetch(
        "CoinGecko BTC market data",
        lambda: _fetch_cg_coin_markets(cg_api_key, "bitcoin"),
        fetch_errors,
    )
    time.sleep(2)

    if cg_btc:
        try:
            btc_price_usd      = cg_btc.get("current_price")
            btc_change_24h     = cg_btc.get("price_change_percentage_24h")
            btc_change_7d      = cg_btc.get("price_change_percentage_7d_in_currency")
            btc_market_cap_usd = cg_btc.get("market_cap")
            btc_volume_24h     = cg_btc.get("total_volume")
            btc_direction      = lean_btc_direction(btc_change_24h)
        except Exception as e:
            fetch_errors.append(f"cg_btc:parse_error: {e}")

    # ------------------------------------------------------------------
    # [5/17] CoinGecko — ETH market data
    # ------------------------------------------------------------------
    print("[5/17] Fetching CoinGecko — ETH market data...")

    eth_price_usd      = None
    eth_change_24h     = None
    eth_change_7d      = None
    eth_market_cap_usd = None
    eth_volume_24h     = None
    eth_btc_ratio      = None

    cg_eth = safe_fetch(
        "CoinGecko ETH market data",
        lambda: _fetch_cg_coin_markets(cg_api_key, "ethereum"),
        fetch_errors,
    )
    time.sleep(2)

    if cg_eth:
        try:
            eth_price_usd      = cg_eth.get("current_price")
            eth_change_24h     = cg_eth.get("price_change_percentage_24h")
            eth_change_7d      = cg_eth.get("price_change_percentage_7d_in_currency")
            eth_market_cap_usd = cg_eth.get("market_cap")
            eth_volume_24h     = cg_eth.get("total_volume")
            if eth_price_usd and btc_price_usd and btc_price_usd != 0:
                eth_btc_ratio = eth_price_usd / btc_price_usd
        except Exception as e:
            fetch_errors.append(f"cg_eth:parse_error: {e}")

    eth_btc_lean = lean_eth_btc(eth_change_7d, btc_change_7d)

    # ------------------------------------------------------------------
    # [6/17] DeFiLlama — total DeFi TVL
    # ------------------------------------------------------------------
    print("[6/17] Fetching DeFiLlama — total DeFi TVL...")

    total_defi_tvl_usd = None

    defillama_chains = safe_fetch(
        "DeFiLlama total DeFi TVL",
        lambda: _fetch_defillama_chains(),
        fetch_errors,
    )
    time.sleep(1)

    if defillama_chains:
        try:
            total_defi_tvl_usd = sum(
                float(c.get("tvl", 0) or 0)
                for c in defillama_chains
                if c.get("tvl") is not None
            )
        except Exception as e:
            fetch_errors.append(f"defillama_tvl:parse_error: {e}")

    # ------------------------------------------------------------------
    # [7/17] DeFiLlama — stablecoin market cap
    # ------------------------------------------------------------------
    print("[7/17] Fetching DeFiLlama — stablecoin market cap...")

    stablecoin_market_cap_usd = None
    stablecoin_dominance_pct  = None

    defillama_stable = safe_fetch(
        "DeFiLlama stablecoin market cap",
        lambda: _fetch_defillama_stablecoins(),
        fetch_errors,
    )
    time.sleep(1)

    if defillama_stable:
        try:
            pegged_list = defillama_stable.get("peggedAssets", [])
            stablecoin_market_cap_usd = sum(
                float(s.get("circulating", {}).get("peggedUSD", 0) or 0)
                for s in pegged_list
            )
            if total_market_cap_usd and total_market_cap_usd != 0:
                stablecoin_dominance_pct = round(
                    (stablecoin_market_cap_usd / total_market_cap_usd) * 100, 4
                )
        except Exception as e:
            fetch_errors.append(f"defillama_stable:parse_error: {e}")

    # ------------------------------------------------------------------
    # [8/17] CMC — global metrics latest
    # ------------------------------------------------------------------
    print("[8/17] Fetching CMC — global metrics latest...")

    cmc_btc_dominance    = None
    cmc_total_mcap_usd   = None
    cmc_total_vol_24h    = None
    cmc_altcoin_mcap     = None
    cmc_defi_vol_24h     = None
    cmc_defi_mcap        = None

    cmc_global = safe_fetch(
        "CMC global metrics latest",
        lambda: _fetch_cmc_global(cmc_api_key),
        fetch_errors,
    )
    time.sleep(2)

    if cmc_global:
        try:
            cmc_btc_dominance  = cmc_global.get("btc_dominance")
            quote_usd          = cmc_global.get("quote", {}).get("USD", {})
            cmc_total_mcap_usd = quote_usd.get("total_market_cap")
            cmc_total_vol_24h  = quote_usd.get("total_volume_24h")
            cmc_altcoin_mcap   = quote_usd.get("altcoin_market_cap")
            cmc_defi_vol_24h   = quote_usd.get("defi_volume_24h")
            cmc_defi_mcap      = quote_usd.get("defi_market_cap")
        except Exception as e:
            fetch_errors.append(f"cmc_global:parse_error: {e}")

    # ------------------------------------------------------------------
    # [9/17] CMC — Fear & Greed latest
    # ------------------------------------------------------------------
    print("[9/17] Fetching CMC — Fear & Greed latest...")

    fg_value = None
    fg_label = None

    cmc_fg = safe_fetch(
        "CMC Fear & Greed latest",
        lambda: _fetch_cmc_fear_greed(cmc_api_key),
        fetch_errors,
    )
    time.sleep(2)

    if cmc_fg:
        try:
            fg_value = int(cmc_fg.get("value", 0))
            fg_label = cmc_fg.get("value_classification")
        except Exception as e:
            fetch_errors.append(f"cmc_fear_greed:parse_error: {e}")

    fear_greed_lean = lean_fear_greed(fg_value)

    # ------------------------------------------------------------------
    # [10/17] RSS — global crypto news feeds
    # ------------------------------------------------------------------
    print("[10/17] Fetching RSS — global crypto news feeds...")

    news_article_count_24h = 0
    news_positive_count    = 0
    news_negative_count    = 0
    news_neutral_count     = 0
    news_sentiment_ratio   = None
    news_volume_lean       = "neutral"
    news_spike             = False
    top_headlines: list    = []

    rss_articles = safe_fetch(
        "RSS global crypto news feeds",
        lambda: fetch_rss_feeds(CRYPTO_RSS_FEEDS, max_age_hours=24),
        fetch_errors,
    )
    time.sleep(1)

    if rss_articles:
        try:
            news_article_count_24h = len(rss_articles)

            for article in rss_articles:
                sentiment = score_sentiment(article["title"])
                polarity  = sentiment["polarity"]
                if polarity == "positive":
                    news_positive_count += 1
                elif polarity == "negative":
                    news_negative_count += 1
                else:
                    news_neutral_count += 1

            if news_article_count_24h > 0:
                news_sentiment_ratio = round(
                    news_positive_count / news_article_count_24h, 4
                )

            if news_sentiment_ratio is not None:
                if news_sentiment_ratio > 0.65:
                    news_volume_lean = "bullish"
                elif news_sentiment_ratio < 0.35:
                    news_volume_lean = "bearish"
                else:
                    news_volume_lean = "neutral"

            news_spike = news_article_count_24h > 20

            for article in rss_articles[:10]:
                sentiment = score_sentiment(article["title"])
                top_headlines.append({
                    "title":        article["title"],
                    "url":          article["url"],
                    "published_at": article["published_at"],
                    "source":       article["source"],
                    "sentiment": {
                        "polarity": sentiment["polarity"],
                        "score":    sentiment["score"],
                    },
                    "is_breaking": False,
                })
        except Exception as e:
            fetch_errors.append(f"RSS global crypto news:parse_error: {e}")
    time.sleep(1)

    # ------------------------------------------------------------------
    # [11/17] Binance Futures — BTC open interest
    # ------------------------------------------------------------------
    print("[11/17] Fetching Binance Futures — BTC open interest...")

    btc_oi_usd = None

    oi_data = safe_fetch(
        "Binance Futures BTC open interest",
        lambda: _fetch_binance_oi("BTCUSDT"),
        fetch_errors,
    )
    time.sleep(1)

    if oi_data and btc_price_usd:
        try:
            btc_oi_usd = float(oi_data["openInterest"]) * btc_price_usd
        except (KeyError, ValueError, TypeError) as e:
            fetch_errors.append(f"binance_btc_oi:parse_error: {e}")

    # ------------------------------------------------------------------
    # [12/17] Binance Futures — BTC funding rate (last 21 obs)
    # ------------------------------------------------------------------
    print("[12/17] Fetching Binance Futures — BTC funding rate...")

    btc_funding_latest = None
    btc_funding_7d_avg = None

    funding_data = safe_fetch(
        "Binance Futures BTC funding rate",
        lambda: _fetch_binance_funding("BTCUSDT"),
        fetch_errors,
    )
    time.sleep(1)

    if funding_data:
        try:
            rates = [float(e["fundingRate"]) for e in funding_data]
            btc_funding_latest = rates[-1]
            btc_funding_7d_avg = sum(rates) / len(rates)
        except (KeyError, ValueError) as e:
            fetch_errors.append(f"binance_btc_funding:parse_error: {e}")

    btc_funding_lean = lean_funding_rate(btc_funding_7d_avg)

    # ------------------------------------------------------------------
    # [13/17] Binance Futures — BTC long/short ratio
    # ------------------------------------------------------------------
    print("[13/17] Fetching Binance Futures — BTC long/short ratio...")

    btc_ls_ratio  = None
    btc_long_pct  = None
    btc_short_pct = None

    ls_data = safe_fetch(
        "Binance Futures BTC long/short ratio",
        lambda: _fetch_binance_ls_ratio(),
        fetch_errors,
    )
    time.sleep(1)

    if ls_data:
        try:
            btc_ls_ratio  = float(ls_data["longShortRatio"])
            btc_long_pct  = float(ls_data["longAccount"]) * 100
            btc_short_pct = float(ls_data["shortAccount"]) * 100
        except (KeyError, ValueError) as e:
            fetch_errors.append(f"binance_btc_ls:parse_error: {e}")

    btc_ls_lean = lean_btc_ls(btc_ls_ratio)

    # ------------------------------------------------------------------
    # [14/17] FRED — DXY latest
    # ------------------------------------------------------------------
    print("[14/17] Fetching FRED — DXY latest (DTWEXBGS)...")

    dxy_latest = None
    dxy_prior  = None

    if fred_api_key:
        dxy_data = safe_fetch(
            "FRED DXY",
            lambda: _fetch_fred_series("DTWEXBGS", fred_api_key, 5),
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

    dxy_lean_val = lean_dxy(dxy_latest, dxy_prior)

    # ------------------------------------------------------------------
    # [15/17] FRED — VIX latest
    # ------------------------------------------------------------------
    print("[15/17] Fetching FRED — VIX latest (VIXCLS)...")

    vix_latest = None
    vix_prior  = None

    if fred_api_key:
        vix_data = safe_fetch(
            "FRED VIX",
            lambda: _fetch_fred_series("VIXCLS", fred_api_key, 5),
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

    vix_lean_val = lean_vix(vix_latest, vix_prior)

    # ------------------------------------------------------------------
    # [16/17] Alpha Vantage — SPY compact
    # ------------------------------------------------------------------
    print("[16/17] Fetching Alpha Vantage — SPY compact...")

    spy_close      = None
    spy_prev_close = None
    spy_change_pct = None

    if alpha_vantage_api_key:
        av_data = safe_fetch(
            "Alpha Vantage SPY",
            lambda: _fetch_alpha_vantage_spy(alpha_vantage_api_key),
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
    # [17/17] Finnhub — macro news (general category)
    # ------------------------------------------------------------------
    print("[17/17] Fetching Finnhub — macro news...")

    finnhub_article_count_24h = 0
    finnhub_headlines: list   = []

    finnhub_api_key = os.getenv("FINNHUB_API_KEY", "").strip()
    if finnhub_api_key:
        fh_data = safe_fetch(
            "Finnhub macro news",
            lambda: _fetch_finnhub_news(finnhub_api_key),
            fetch_errors,
        )
        if fh_data is not None:
            try:
                cutoff = time.time() - 86400
                recent = [a for a in fh_data if a.get("datetime", 0) >= cutoff]
                finnhub_article_count_24h = len(recent)
                sorted_recent = sorted(recent, key=lambda a: a.get("datetime", 0), reverse=True)
                for article in sorted_recent[:10]:
                    ts = article.get("datetime", 0)
                    published_at = datetime.utcfromtimestamp(ts).strftime("%Y-%m-%dT%H:%M:%SZ")
                    finnhub_headlines.append({
                        "headline":     article.get("headline", ""),
                        "summary":      article.get("summary", ""),
                        "source":       article.get("source", ""),
                        "url":          article.get("url", ""),
                        "published_at": published_at,
                    })
            except Exception as e:
                fetch_errors.append(f"finnhub_news:parse_error: {e}")
    else:
        print("  \u2717 Finnhub macro news — FINNHUB_API_KEY not set")
        fetch_errors.append("fetch_17:finnhub_news: skipped — FINNHUB_API_KEY not set")
    time.sleep(1)

    # ------------------------------------------------------------------
    # Compute signals
    # ------------------------------------------------------------------
    market_cap_direction_lean = lean_market_cap_direction(total_market_cap_change_24h)
    btc_dominance_lean        = lean_btc_dominance(btc_dominance_pct)
    btc_direction_lean        = lean_btc_direction(btc_change_24h)
    spy_lean_val              = lean_spy(spy_change_pct)

    signals = {
        "market_cap_direction_lean": market_cap_direction_lean,
        "btc_dominance_lean":        btc_dominance_lean,
        "altcoin_season_lean":       altcoin_season_lean,
        "btc_direction_lean":        btc_direction_lean,
        "btc_funding_lean":          btc_funding_lean,
        "btc_ls_lean":               btc_ls_lean,
        "eth_btc_lean":              eth_btc_lean,
        "dxy_lean":                  dxy_lean_val,
        "spy_lean":                  spy_lean_val,
        "vix_lean":                  vix_lean_val,
        "fear_greed_lean":           fear_greed_lean,
        "news_volume_lean":          news_volume_lean,
    }

    available_sigs = {k: v for k, v in signals.items() if v is not None}
    null_sigs      = {k: v for k, v in signals.items() if v is None}
    bullish_count  = sum(1 for v in available_sigs.values() if v == "bullish")
    bearish_count  = sum(1 for v in available_sigs.values() if v == "bearish")
    neutral_count  = sum(1 for v in available_sigs.values() if v == "neutral")
    overall_lean   = compute_overall_lean(signals)

    # ------------------------------------------------------------------
    # Assemble report
    # ------------------------------------------------------------------
    return {
        "report_metadata": {
            "generated_at":   now_utc.strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "report_type":    "global",
            "script_version": SCRIPT_VERSION,
            "schema_version": SCHEMA_VERSION,
        },
        "market_structure": {
            "total_market_cap_usd":           round(total_market_cap_usd, 2)        if total_market_cap_usd        is not None else None,
            "total_market_cap_change_24h_pct": round(total_market_cap_change_24h, 4) if total_market_cap_change_24h is not None else None,
            "total_volume_24h_usd":            round(total_volume_24h_usd, 2)        if total_volume_24h_usd        is not None else None,
            "btc_dominance_pct":               round(btc_dominance_pct, 4)           if btc_dominance_pct           is not None else None,
            "eth_dominance_pct":               round(eth_dominance_pct, 4)           if eth_dominance_pct           is not None else None,
            "total2_usd":                      round(total2_usd, 2)                  if total2_usd                  is not None else None,
            "total3_usd":                      round(total3_usd, 2)                  if total3_usd                  is not None else None,
            "active_cryptocurrencies":         active_cryptocurrencies,
            "altcoin_season_index":            altcoin_season_index,
            "altcoin_season_label":            altcoin_season_label,
            "altcoin_season_lean":             altcoin_season_lean,
            "altcoin_season_data_note":        "Computed from 7d returns (CoinGecko free tier does not support 90d window)",
            "top_gainers":                     top_gainers,
            "top_losers":                      top_losers,
        },
        "btc": {
            "price_usd":            round(btc_price_usd, 2)      if btc_price_usd      is not None else None,
            "price_change_24h_pct": round(btc_change_24h, 4)     if btc_change_24h     is not None else None,
            "price_change_7d_pct":  round(btc_change_7d, 4)      if btc_change_7d      is not None else None,
            "market_cap_usd":       round(btc_market_cap_usd, 2) if btc_market_cap_usd is not None else None,
            "volume_24h_usd":       round(btc_volume_24h, 2)     if btc_volume_24h     is not None else None,
            "direction":            btc_direction,
            "oi_usd":               round(btc_oi_usd, 2)         if btc_oi_usd         is not None else None,
            "funding_rate_latest":  btc_funding_latest,
            "funding_rate_7d_avg":  btc_funding_7d_avg,
            "funding_lean":         btc_funding_lean,
            "long_short_ratio":     round(btc_ls_ratio, 4)       if btc_ls_ratio       is not None else None,
            "long_account_pct":     round(btc_long_pct, 4)       if btc_long_pct       is not None else None,
            "short_account_pct":    round(btc_short_pct, 4)      if btc_short_pct      is not None else None,
            "btc_ls_lean":          btc_ls_lean,
        },
        "eth": {
            "price_usd":            round(eth_price_usd, 2)      if eth_price_usd      is not None else None,
            "price_change_24h_pct": round(eth_change_24h, 4)     if eth_change_24h     is not None else None,
            "price_change_7d_pct":  round(eth_change_7d, 4)      if eth_change_7d      is not None else None,
            "market_cap_usd":       round(eth_market_cap_usd, 2) if eth_market_cap_usd is not None else None,
            "volume_24h_usd":       round(eth_volume_24h, 2)     if eth_volume_24h     is not None else None,
            "eth_btc_ratio":        round(eth_btc_ratio, 6)      if eth_btc_ratio      is not None else None,
            "eth_btc_lean":         eth_btc_lean,
        },
        "defi": {
            "total_defi_tvl_usd":        round(total_defi_tvl_usd, 2)        if total_defi_tvl_usd        is not None else None,
            "stablecoin_market_cap_usd": round(stablecoin_market_cap_usd, 2) if stablecoin_market_cap_usd is not None else None,
            "stablecoin_dominance_pct":  stablecoin_dominance_pct,
        },
        "macro_news": {
            "source":            "finnhub",
            "article_count_24h": finnhub_article_count_24h,
            "headlines":         finnhub_headlines,
        },
        "macro": {
            "dxy":            round(dxy_latest, 4)    if dxy_latest    is not None else None,
            "dxy_lean":       dxy_lean_val,
            "spy_close":      round(spy_close, 4)     if spy_close     is not None else None,
            "spy_change_pct": round(spy_change_pct, 4) if spy_change_pct is not None else None,
            "spy_lean":       spy_lean_val,
            "vix_close":      round(vix_latest, 4)   if vix_latest    is not None else None,
            "vix_lean":       vix_lean_val,
        },
        "sentiment": {
            "fear_greed_value": fg_value,
            "fear_greed_label": fg_label,
            "fear_greed_lean":  fear_greed_lean,
        },
        "news": {
            "source":           "rss",
            "article_count_24h": news_article_count_24h,
            "positive_count":    news_positive_count,
            "negative_count":    news_negative_count,
            "neutral_count":     news_neutral_count,
            "sentiment_ratio":   news_sentiment_ratio,
            "news_volume_lean":  news_volume_lean,
            "news_spike":        news_spike,
            "top_headlines":     top_headlines,
        },
        "signal_summary": {
            "signals_evaluated": 12,
            "signals_available": len(available_sigs),
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
# Individual fetch functions
# ---------------------------------------------------------------------------

def _fetch_cg_global(api_key: str) -> dict:
    url = f"{CG_BASE}/global"
    resp = requests.get(url, headers=_cg_headers(api_key), timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError("Empty global response")
    return data


def _fetch_cg_top100(api_key: str) -> list:
    url = f"{CG_BASE}/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 100,
        "page": 1,
        "price_change_percentage": "7d",
        "sparkline": "false",
    }
    resp = requests.get(url, headers=_cg_headers(api_key), params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError("Empty top 100 response")
    return data


def _fetch_cg_markets_250(api_key: str) -> list:
    url = f"{CG_BASE}/coins/markets"
    params = {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 250,
        "page": 1,
        "price_change_percentage": "24h",
        "sparkline": "false",
    }
    resp = requests.get(url, headers=_cg_headers(api_key), params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError("Empty markets 250 response")
    return data


def _fetch_cg_coin_markets(api_key: str, coin_id: str) -> dict:
    url = f"{CG_BASE}/coins/markets"
    params = {
        "vs_currency": "usd",
        "ids": coin_id,
        "price_change_percentage": "24h,7d",
    }
    resp = requests.get(url, headers=_cg_headers(api_key), params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Empty markets response for {coin_id}")
    return data[0]


def _fetch_defillama_chains() -> list:
    url = "https://api.llama.fi/v2/chains"
    resp = requests.get(url, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError("Empty DeFiLlama chains response")
    return data


def _fetch_defillama_stablecoins() -> dict:
    url = "https://stablecoins.llama.fi/stablecoins?includePrices=true"
    resp = requests.get(url, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError("Empty DeFiLlama stablecoins response")
    return data


def _fetch_cmc_global(api_key: str) -> dict:
    url = f"{CMC_BASE}/v1/global-metrics/quotes/latest"
    resp = requests.get(url, headers=_cmc_headers(api_key), timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json().get("data", {})
    if not data:
        raise ValueError("Empty CMC global metrics response")
    return data


def _fetch_cmc_fear_greed(api_key: str) -> dict:
    url = f"{CMC_BASE}/v3/fear-and-greed/latest"
    resp = requests.get(url, headers=_cmc_headers(api_key), timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json().get("data", {})
    if not data:
        raise ValueError("Empty CMC Fear & Greed response")
    return data




def _fetch_binance_oi(symbol: str) -> dict:
    url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={symbol}"
    resp = requests.get(url, timeout=API_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _fetch_binance_funding(symbol: str) -> list:
    url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=21"
    resp = requests.get(url, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError(f"Empty funding rate for {symbol}")
    return data


def _fetch_binance_ls_ratio() -> dict:
    url = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio?symbol=BTCUSDT&period=1h&limit=1"
    resp = requests.get(url, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise ValueError("Empty BTC L/S ratio response")
    return data[0]


def _fetch_fred_series(series_id: str, api_key: str, limit: int = 5) -> list:
    url = f"{FRED_BASE}/series/observations"
    params = {
        "series_id": series_id,
        "sort_order": "desc",
        "limit": limit,
        "api_key": api_key,
        "file_type": "json",
    }
    resp = requests.get(url, params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("observations", [])


def _fetch_finnhub_news(api_key: str) -> list:
    url = "https://finnhub.io/api/v1/news"
    params = {"category": "general", "token": api_key}
    resp = requests.get(url, params=params, timeout=API_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise ValueError("Unexpected Finnhub response format")
    return data


def _fetch_alpha_vantage_spy(api_key: str) -> dict:
    url = "https://www.alphavantage.co/query"
    params = {
        "function": "TIME_SERIES_DAILY",
        "symbol": "SPY",
        "outputsize": "compact",
        "apikey": api_key,
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    ts = resp.json().get("Time Series (Daily)", {})
    if not ts:
        raise ValueError("No time series data in Alpha Vantage response")
    return ts


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
        description=f"Fetch global crypto market data and produce a structured report (v{SCRIPT_VERSION})."
    )
    parser.add_argument(
        "--md",
        action="store_true",
        help="Also output a Markdown report (saved to reports/)",
    )
    args = parser.parse_args()

    script_dir  = Path(__file__).parent.resolve()
    reports_dir = script_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    env_path = script_dir / ".env"
    load_dotenv(dotenv_path=env_path)

    def _key(name: str, required: bool = False) -> str:
        val = os.environ.get(name, "").strip()
        if not val:
            if required:
                print(f"ERROR: {name} is not set — cannot continue.", file=sys.stderr)
                sys.exit(1)
            print(f"WARNING: {name} is not set — related fetches will be skipped.", file=sys.stderr)
        return val

    cg_api_key            = _key("COINGECKO_API_KEY", required=True)
    cmc_api_key           = _key("CMC_API_KEY", required=True)
    fred_api_key          = _key("FRED_API_KEY")
    alpha_vantage_api_key = _key("ALPHA_VANTAGE_API_KEY")

    print(f"Fetching global crypto market data (v{SCRIPT_VERSION})...")

    report = build_report(
        cg_api_key=cg_api_key,
        cmc_api_key=cmc_api_key,
        fred_api_key=fred_api_key,
        alpha_vantage_api_key=alpha_vantage_api_key,
    )

    ts        = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    json_path = reports_dir / f"market_report_global_{ts}.json"
    md_path   = reports_dir / f"market_report_global_{ts}.md"

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\nReport saved: {json_path}")

    # Push to GitHub (non-blocking)
    push_report_to_github(str(json_path))

    if args.md:
        md_content = render_markdown(report)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"Markdown report saved: {md_path}")
        push_report_to_github(str(md_path))
        print()
        print(md_content)

    if report["fetch_errors"]:
        print("\nFetch errors/notes:", file=sys.stderr)
        for e in report["fetch_errors"]:
            print(f"  - {e}", file=sys.stderr)

    sys.exit(0)


if __name__ == "__main__":
    main()
