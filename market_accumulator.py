# Muneo Market Accumulator - A script to accumulate daily market reports into weekly, monthly, and quarterly summaries.

import os
import json
import base64
import argparse
import shutil
from datetime import datetime, timezone
from dateutil.parser import parse as parse_date
from dotenv import load_dotenv
from pathlib import Path
import requests

load_dotenv()

# Accumulation thresholds
MIN_DAYS_FOR_WEEKLY = 4       # Minimum daily reports needed to generate a weekly report
MIN_WEEKS_FOR_MONTHLY = 3     # Minimum weekly reports needed to generate a monthly report
MIN_MONTHS_FOR_QUARTERLY = 1  # Minimum monthly reports needed to generate a partial quarterly report

# Retention thresholds (number of files to keep in reports/ before archiving/deleting)
RETAIN_DAILY = 14             # Keep last 14 daily reports locally
RETAIN_WEEKLY = 7             # Keep last 7 weekly reports locally
RETAIN_MONTHLY = 3            # Keep last 3 monthly reports locally
# Quarterly reports are never archived or deleted

SCRIPT_VERSION = "1.0.0"
SCHEMA_VERSION = "1.0.0"

def parse_args():
    parser = argparse.ArgumentParser(
        description="Muneo Market Accumulator — rolls up daily reports into weekly/monthly/quarterly summaries"
    )
    parser.add_argument(
        "--config",
        default="configs/sui.json",
        help="Path to token config file (default: configs/sui.json)"
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Clear all accumulated output and regenerate from scratch"
    )
    parser.add_argument(
        "--md",
        action="store_true",
        help="Write Markdown summaries alongside JSON output"
    )
    return parser.parse_args()

def load_config(config_path: str) -> dict:
    """Load and validate token config file."""
    with open(config_path, "r") as f:
        config = json.load(f)
    
    required_fields = ["token_name", "output_prefix"]
    for field in required_fields:
        if field not in config:
            raise ValueError(f"Config missing required field: {field}")
    
    return config

def setup_directories(config: dict) -> dict:
    """Create output directories if they don't exist. Returns path dict."""
    token_dir = config["token_name"].lower()  # e.g. "sui" or "sol"
    
    paths = {
        "reports": Path("./reports"),
        "archive": Path("./reports/archive"),
        "context_root": Path(f"./context/{token_dir}"),
        "accumulated_base": Path(f"./context/{token_dir}/accumulated"),
        "weekly": Path(f"./context/{token_dir}/accumulated/weekly"),
        "monthly": Path(f"./context/{token_dir}/accumulated/monthly"),
        "quarterly": Path(f"./context/{token_dir}/accumulated/quarterly"),
    }
    
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    
    return paths

def discover_daily_reports(reports_dir: Path, output_prefix: str) -> list[dict]:
    results = []
    for f in reports_dir.glob("*.json"):
        if not f.name.startswith(output_prefix):
            continue
        if "archive" in f.parts:
            continue
        ts_str = f.name.replace(output_prefix + "_", "").replace(".json", "")
        try:
            dt = datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
        except ValueError:
            print(f"  ⚠ Skipping unparseable filename: {f.name}")
            continue
        iso = dt.isocalendar()
        results.append({
            "path": f,
            "filename": f.name,
            "timestamp": dt,
            "date_str": dt.strftime("%Y-%m-%d"),
            "iso_year": iso.year,
            "iso_week": iso.week,
            "iso_weekday": iso.weekday
        })
    return sorted(results, key=lambda x: x["timestamp"])

def group_by_iso_week(daily_reports: list[dict]) -> dict:
    from collections import defaultdict
    groups = defaultdict(list)
    for report in daily_reports:
        key = (report["iso_year"], report["iso_week"])
        groups[key].append(report)
    return dict(sorted(groups.items()))

def load_daily_report(path: Path) -> dict | None:
    """Load a daily report JSON. Returns None on failure."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"  ⚠ Failed to load {path.name}: {e}")
        return None

def deduplicate_by_date(daily_reports: list[dict]) -> list[dict]:
    """
    If multiple reports exist for the same date, keep only the most recent.
    Returns deduplicated list sorted by timestamp ascending.
    """
    by_date = {}
    for r in daily_reports:
        date = r["date_str"]
        if date not in by_date or r["timestamp"] > by_date[date]["timestamp"]:
            by_date[date] = r
    return sorted(by_date.values(), key=lambda x: x["timestamp"])

def aggregate_weekly_price(daily_data: list[dict]) -> dict:
    def vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        return result

    prices = vals("price_data.price_usd")
    volumes = vals("price_data.volume_24h_usd")
    mcaps = vals("price_data.market_cap_usd")
    cex_prices = vals("price_data.cex_price_usd")
    up_vols = vals("price_data.volume_on_up_days_usd")
    down_vols = vals("price_data.volume_on_down_days_usd")
    volume_leans = vals("price_data.volume_lean")

    open_usd = prices[0] if prices else None
    close_usd = prices[-1] if prices else None

    from statistics import mode, mean
    return {
        "open_usd": open_usd,
        "close_usd": close_usd,
        "high_usd": max(prices) if prices else None,
        "low_usd": min(prices) if prices else None,
        "price_change_week_pct": round((close_usd - open_usd) / open_usd * 100, 4) if open_usd and close_usd else None,
        "avg_volume_24h_usd": round(mean(volumes), 2) if volumes else None,
        "total_volume_week_usd": round(sum(volumes), 2) if volumes else None,
        "avg_market_cap_usd": round(mean(mcaps), 2) if mcaps else None,
        "close_market_cap_usd": mcaps[-1] if mcaps else None,
        "ath_usd": vals("price_data.ath_usd")[-1] if vals("price_data.ath_usd") else None,
        "ath_drawdown_pct": vals("price_data.ath_drawdown_pct")[-1] if vals("price_data.ath_drawdown_pct") else None,
        "avg_cex_price_usd": round(mean(cex_prices), 6) if cex_prices else None,
        "volume_on_up_days_usd": round(sum(up_vols), 2) if up_vols else None,
        "volume_on_down_days_usd": round(sum(down_vols), 2) if down_vols else None,
        "volume_lean": mode(volume_leans) if volume_leans else None,
        "days_included": len(daily_data)
    }

def aggregate_weekly_technicals(daily_data: list[dict]) -> dict:
    def last_val(field_path):
        keys = field_path.split(".")
        for d in reversed(daily_data):
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    rsi = last_val("technical_indicators.rsi_14")
    sma_50 = last_val("technical_indicators.sma_50")
    ema_200 = last_val("technical_indicators.ema_200")
    close_price = last_val("price_data.price_usd")
    price_vs_sma50_pct = round((close_price - sma_50) / sma_50 * 100, 4) if close_price and sma_50 else None
    price_vs_ema200_pct = round((close_price - ema_200) / ema_200 * 100, 4) if close_price and ema_200 else None
    bb = last_val("technical_indicators.bollinger_bands")

    return {
        "rsi_14_at_close": rsi,
        "rsi_lean": last_val("technical_indicators.rsi_lean"),
        "macd_at_close": {
            "macd_line": last_val("technical_indicators.macd.macd_line"),
            "signal_line": last_val("technical_indicators.macd.signal_line"),
            "histogram": last_val("technical_indicators.macd.histogram"),
        },
        "macd_lean": last_val("technical_indicators.macd_lean"),
        "bollinger_bands_at_close": bb,
        "bb_lean": last_val("technical_indicators.bb_lean"),
        "sma_50_at_close": sma_50,
        "price_vs_sma50_pct": price_vs_sma50_pct,
        "ema_200_at_close": ema_200,
        "price_vs_ema200_pct": price_vs_ema200_pct,
        "ema200_lean": last_val("technical_indicators.ema200_lean"),
    }

def aggregate_weekly_derivatives(daily_data: list[dict]) -> dict:
    def last_val(field_path):
        keys = field_path.split(".")
        for d in reversed(daily_data):
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def first_val(field_path):
        keys = field_path.split(".")
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def avg_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        if not result:
            return None
        from statistics import mean
        return round(mean(result), 8)

    oi_open = first_val("derivatives.oi_usd")
    oi_close = last_val("derivatives.oi_usd")
    oi_change_pct = round((oi_close - oi_open) / oi_open * 100, 4) if oi_open and oi_close else None

    return {
        "oi_at_close_usd": oi_close,
        "oi_open_usd": oi_open,
        "oi_change_pct": oi_change_pct,
        "oi_lean": last_val("derivatives.oi_lean"),
        "funding_rate_daily_avg": avg_vals("derivatives.funding_rate_daily_avg"),
        "funding_rate_7d_rolling_avg": last_val("derivatives.funding_rate_7d_rolling_avg"),
        "funding_lean": last_val("derivatives.funding_lean"),
        "btc_long_short_ratio": last_val("derivatives.btc_long_short_ratio"),
        "btc_long_account_pct": last_val("derivatives.btc_long_account_pct"),
        "btc_short_account_pct": last_val("derivatives.btc_short_account_pct"),
        "btc_ls_ratio_lean": last_val("derivatives.btc_ls_ratio_lean"),
    }

def aggregate_weekly_on_chain(daily_data: list[dict]) -> dict:
    def last_val(field_path):
        keys = field_path.split(".")
        for d in reversed(daily_data):
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def first_val(field_path):
        keys = field_path.split(".")
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def avg_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        if not result:
            return None
        from statistics import mean
        return round(mean(result), 2)

    def sum_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        if not result:
            return None
        return round(sum(result), 2)

    tvl_start = first_val("on_chain.tvl_usd")
    tvl_end = last_val("on_chain.tvl_usd")
    tvl_change_pct = round((tvl_end - tvl_start) / tvl_start * 100, 4) if tvl_start and tvl_end else None

    addr_start = first_val("on_chain.active_addresses")
    addr_end = last_val("on_chain.active_addresses")
    if addr_start and addr_end:
        if addr_end > addr_start * 1.02:
            addr_lean = "bullish"
        elif addr_end < addr_start * 0.98:
            addr_lean = "bearish"
        else:
            addr_lean = "neutral"
    else:
        addr_lean = None

    net_flow = sum_vals("on_chain.exchange_net_flow_usd")
    if net_flow is not None:
        exchange_flow_lean = "bearish" if net_flow > 0 else "bullish" if net_flow < 0 else "neutral"
    else:
        exchange_flow_lean = None

    tvl_lean = None
    if tvl_change_pct is not None:
        if tvl_change_pct > 2:
            tvl_lean = "bullish"
        elif tvl_change_pct < -2:
            tvl_lean = "bearish"
        else:
            tvl_lean = "neutral"

    return {
        "tvl_start_usd": tvl_start,
        "tvl_end_usd": tvl_end,
        "tvl_change_pct": tvl_change_pct,
        "tvl_direction": "rising" if tvl_change_pct and tvl_change_pct > 0 else "falling" if tvl_change_pct and tvl_change_pct < 0 else "flat" if tvl_change_pct == 0 else None,
        "tvl_lean": tvl_lean,
        "active_addresses_avg": avg_vals("on_chain.active_addresses"),
        "active_addresses_start": addr_start,
        "active_addresses_end": addr_end,
        "active_addresses_lean": addr_lean,
        "exchange_inflow_avg_usd": avg_vals("on_chain.exchange_inflow_usd"),
        "exchange_outflow_avg_usd": avg_vals("on_chain.exchange_outflow_usd"),
        "exchange_net_flow_usd": net_flow,
        "exchange_flow_lean": exchange_flow_lean,
    }

def aggregate_weekly_macro(daily_data: list[dict]) -> dict:
    def last_val(field_path):
        keys = field_path.split(".")
        for d in reversed(daily_data):
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def first_val(field_path):
        keys = field_path.split(".")
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def avg_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        if not result:
            return None
        from statistics import mean
        return round(mean(result), 4)

    def pct_change(start, end):
        if start and end:
            return round((end - start) / start * 100, 4)
        return None

    def direction(start, end):
        if start is None or end is None:
            return None
        if end > start * 1.001:
            return "rising"
        elif end < start * 0.999:
            return "falling"
        return "flat"

    # BTC
    btc_start = first_val("macro.btc_price_usd")
    btc_end = last_val("macro.btc_price_usd")
    token_start = first_val("price_data.price_usd")
    token_end = last_val("price_data.price_usd")
    btc_return = pct_change(btc_start, btc_end)
    token_return = pct_change(token_start, token_end)
    alpha = round(token_return - btc_return, 4) if token_return is not None and btc_return is not None else None

    btc_direction = direction(btc_start, btc_end)
    btc_dir_lean = "bullish" if btc_direction == "rising" else "bearish" if btc_direction == "falling" else "neutral" if btc_direction == "flat" else None

    # BTC dominance
    dom_start = first_val("macro.btc_dominance")
    dom_end = last_val("macro.btc_dominance")
    dom_dir = direction(dom_start, dom_end)
    dom_lean = "bearish" if dom_dir == "rising" else "bullish" if dom_dir == "falling" else "neutral" if dom_dir == "flat" else None

    # Total crypto mcap
    mcap_start = first_val("macro.total_crypto_mcap_usd")
    mcap_end = last_val("macro.total_crypto_mcap_usd")

    # ETH/BTC ratio
    eth_btc_start = first_val("macro.eth_btc_ratio")
    eth_btc_end = last_val("macro.eth_btc_ratio")
    eth_btc_lean = "bullish" if eth_btc_end and eth_btc_start and eth_btc_end > eth_btc_start else "bearish" if eth_btc_end and eth_btc_start and eth_btc_end < eth_btc_start else "neutral" if eth_btc_start and eth_btc_end else None

    # DXY
    dxy_start = first_val("macro.dxy")
    dxy_end = last_val("macro.dxy")
    dxy_change = pct_change(dxy_start, dxy_end)
    dxy_dir = direction(dxy_start, dxy_end)
    dxy_lean = "bearish" if dxy_dir == "rising" else "bullish" if dxy_dir == "falling" else "neutral" if dxy_dir == "flat" else None

    # SPY
    spy_start = first_val("macro.spy_close")
    spy_end = last_val("macro.spy_close")
    spy_change = pct_change(spy_start, spy_end)
    spy_lean = "bullish" if spy_change and spy_change > 0 else "bearish" if spy_change and spy_change < 0 else "neutral" if spy_change == 0 else None

    # VIX
    vix_start = first_val("macro.vix_close")
    vix_end = last_val("macro.vix_close")
    vix_avg = avg_vals("macro.vix_close")
    if vix_end is not None and vix_avg is not None:
        if vix_end < 20 and (vix_start is None or vix_end <= vix_start):
            vix_lean = "bullish"
        elif vix_end > 25 or (vix_start is not None and vix_end > vix_start):
            vix_lean = "bearish"
        else:
            vix_lean = "neutral"
    else:
        vix_lean = None

    return {
        "btc_period_return_pct": btc_return,
        "btc_direction": btc_direction,
        "token_vs_btc_alpha_pct": alpha,
        "btc_dominance_start": dom_start,
        "btc_dominance_end": dom_end,
        "btc_dominance_direction": dom_dir,
        "btc_dominance_lean": dom_lean,
        "total_crypto_mcap_start_usd": mcap_start,
        "total_crypto_mcap_end_usd": mcap_end,
        "eth_btc_ratio_start": eth_btc_start,
        "eth_btc_ratio_end": eth_btc_end,
        "eth_btc_lean": eth_btc_lean,
        "dxy_start": dxy_start,
        "dxy_end": dxy_end,
        "dxy_change_pct": dxy_change,
        "dxy_lean": dxy_lean,
        "spy_start": spy_start,
        "spy_end": spy_end,
        "spy_change_pct": spy_change,
        "spy_lean": spy_lean,
        "vix_start": vix_start,
        "vix_end": vix_end,
        "vix_avg": vix_avg,
        "vix_lean": vix_lean,
    }

def aggregate_weekly_sentiment(daily_data: list[dict]) -> dict:
    def last_val(field_path):
        keys = field_path.split(".")
        for d in reversed(daily_data):
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                return v
        return None

    def avg_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        if not result:
            return None
        from statistics import mean
        return round(mean(result), 4)

    def min_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        return min(result) if result else None

    def max_vals(field_path):
        keys = field_path.split(".")
        result = []
        for d in daily_data:
            v = d
            for k in keys:
                v = (v or {}).get(k)
            if v is not None:
                result.append(v)
        return max(result) if result else None

    fg_at_close = last_val("sentiment.fear_greed_value")
    fg_label = last_val("sentiment.fear_greed_label")

    if fg_at_close is not None:
        if fg_at_close >= 60:
            fg_lean = "bearish"
        elif fg_at_close <= 40:
            fg_lean = "bullish"
        else:
            fg_lean = "neutral"
    else:
        fg_lean = None

    lc_sentiment_close = last_val("sentiment.lc_sentiment")
    if lc_sentiment_close is not None:
        if lc_sentiment_close >= 60:
            sentiment_lean = "bullish"
        elif lc_sentiment_close <= 40:
            sentiment_lean = "bearish"
        else:
            sentiment_lean = "neutral"
    else:
        sentiment_lean = None

    galaxy_score_close = last_val("sentiment.lc_galaxy_score")
    if galaxy_score_close is not None:
        if galaxy_score_close >= 60:
            galaxy_score_lean = "bullish"
        elif galaxy_score_close <= 40:
            galaxy_score_lean = "bearish"
        else:
            galaxy_score_lean = "neutral"
    else:
        galaxy_score_lean = None

    return {
        "fear_greed_avg": avg_vals("sentiment.fear_greed_value"),
        "fear_greed_min": min_vals("sentiment.fear_greed_value"),
        "fear_greed_max": max_vals("sentiment.fear_greed_value"),
        "fear_greed_at_close": fg_at_close,
        "fear_greed_label_at_close": fg_label,
        "fear_greed_lean": fg_lean,
        "lc_interactions_avg": avg_vals("sentiment.lc_interactions"),
        "lc_sentiment_avg": avg_vals("sentiment.lc_sentiment"),
        "lc_sentiment_at_close": lc_sentiment_close,
        "lc_posts_active_avg": avg_vals("sentiment.lc_posts_active"),
        "lc_contributors_active_avg": avg_vals("sentiment.lc_contributors_active"),
        "lc_galaxy_score_avg": avg_vals("sentiment.lc_galaxy_score"),
        "lc_galaxy_score_at_close": galaxy_score_close,
        "galaxy_score_lean": galaxy_score_lean,
        "lc_alt_rank_at_close": last_val("sentiment.lc_alt_rank"),
        "lc_social_dominance_avg": avg_vals("sentiment.lc_social_dominance"),
        "sentiment_lean": sentiment_lean,
    }

def aggregate_weekly_news(daily_data: list[dict]) -> dict:
    """
    Aggregate news headlines from daily reports into a weekly summary.
    Collects all headlines across the week, deduplicates by title,
    and returns the most recent up to a capped limit.
    """
    MAX_HEADLINES = 20

    seen_titles = set()
    all_headlines = []

    for d in daily_data:
        news = (d or {}).get("news", {})
        if not news:
            continue
        for source_key in ("cryptopanic", "finnhub"):
            items = news.get(source_key) or []
            for item in items:
                title = item.get("title") or item.get("headline")
                if not title:
                    continue
                if title in seen_titles:
                    continue
                seen_titles.add(title)
                all_headlines.append({
                    "title": title,
                    "source": source_key,
                    "published_at": item.get("published_at") or item.get("datetime"),
                    "url": item.get("url"),
                    "sentiment": item.get("votes", {}).get("sentiment") if source_key == "cryptopanic" else None,
                })

    # Sort by published_at descending, keep most recent up to cap
    def sort_key(h):
        ts = h.get("published_at")
        return ts if ts else ""

    all_headlines.sort(key=sort_key, reverse=True)
    all_headlines = all_headlines[:MAX_HEADLINES]

    return {
        "headline_count": len(all_headlines),
        "sources_included": list({h["source"] for h in all_headlines}),
        "headlines": all_headlines,
    }

def aggregate_weekly_signal_summary(daily_data: list[dict]) -> dict:
    """
    Aggregate signal summary from daily reports into a weekly summary.
    Period-end lean is taken from the last daily report.
    Daily counts are tallied across all days in the period.
    signal_streaks tracks consecutive lean runs across the week.
    """
    SIGNAL_KEYS = [
        "rsi_lean", "macd_lean", "ema200_lean", "bb_lean", "volume_lean",
        "oi_lean", "funding_lean", "tvl_lean", "exchange_flow_lean",
        "active_addresses_lean", "btc_dominance_lean", "eth_btc_lean",
        "dxy_lean", "spy_lean", "vix_lean", "fear_greed_lean",
        "sentiment_lean", "galaxy_score_lean", "btc_ls_ratio_lean",
    ]

    # Period-end signals from last daily report
    def last_signal(key):
        for d in reversed(daily_data):
            sig = (d or {}).get("signal_summary", {}).get("signals", {})
            val = sig.get(key)
            if val is not None:
                return val
        return None

    period_end_signals = {k: last_signal(k) for k in SIGNAL_KEYS}

    # Count non-null signals
    available = [k for k, v in period_end_signals.items() if v is not None]
    null_signals = [k for k, v in period_end_signals.items() if v is None]
    bullish = sum(1 for v in period_end_signals.values() if v == "bullish")
    bearish = sum(1 for v in period_end_signals.values() if v == "bearish")
    neutral = sum(1 for v in period_end_signals.values() if v == "neutral")

    if len(available) == 0:
        overall_lean = None
    elif bullish > bearish and bullish > neutral:
        overall_lean = "bullish"
    elif bearish > bullish and bearish > neutral:
        overall_lean = "bearish"
    else:
        overall_lean = "neutral"

    # Signal streaks: track overall_lean across each daily report in the week
    daily_leans = []
    for d in daily_data:
        lean = (d or {}).get("signal_summary", {}).get("overall_lean")
        daily_leans.append(lean)

    current_streak_lean = None
    current_streak_length = 0
    longest_bullish = 0
    longest_bearish = 0

    if daily_leans:
        # Current streak: count from end backwards
        current_streak_lean = daily_leans[-1]
        for lean in reversed(daily_leans):
            if lean == current_streak_lean:
                current_streak_length += 1
            else:
                break

        # Longest streaks across period
        streak = 1
        for i in range(1, len(daily_leans)):
            if daily_leans[i] == daily_leans[i - 1] and daily_leans[i] is not None:
                streak += 1
            else:
                streak = 1
            if daily_leans[i] == "bullish":
                longest_bullish = max(longest_bullish, streak)
            elif daily_leans[i] == "bearish":
                longest_bearish = max(longest_bearish, streak)

        # Check first day too
        if daily_leans[0] == "bullish":
            longest_bullish = max(longest_bullish, 1)
        elif daily_leans[0] == "bearish":
            longest_bearish = max(longest_bearish, 1)

    return {
        "signals_evaluated": len(SIGNAL_KEYS),
        "signals_available": len(available),
        "signals_null": len(null_signals),
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "overall_lean": overall_lean,
        "signals": period_end_signals,
        "signal_streaks": {
            "current_streak_lean": current_streak_lean,
            "current_streak_length": current_streak_length,
            "longest_bullish_streak_in_period": longest_bullish,
            "longest_bearish_streak_in_period": longest_bearish,
        },
    }

def aggregate_accumulation_metadata(daily_reports: list[dict], daily_data: list[dict]) -> dict:
    """
    Compute accumulation_metadata block for a weekly report.
    daily_reports: list of file metadata dicts (with date_str keys).
    daily_data: list of loaded daily report JSON dicts.
    """
    constituent_periods = 7
    constituent_available = len(daily_data)
    constituent_missing = constituent_periods - constituent_available

    if constituent_missing == 0:
        data_quality = "complete"
    elif constituent_available >= MIN_DAYS_FOR_WEEKLY:
        data_quality = "partial"
    else:
        data_quality = "insufficient"

    return {
        "source": "accumulated",
        "constituent_periods": constituent_periods,
        "constituent_periods_available": constituent_available,
        "constituent_periods_missing": constituent_missing,
        "data_quality": data_quality,
    }


def aggregate_cex_dex_spread(daily_data: list[dict]) -> dict:
    """
    Compute CEX/DEX spread stats across the week.
    Spread = abs((cex_price_usd - price_usd) / price_usd * 100)
    """
    spreads = []
    for d in daily_data:
        price = (d or {}).get("price_data", {}).get("price_usd")
        cex_price = (d or {}).get("price_data", {}).get("cex_price_usd")
        if price and cex_price and price > 0:
            spread_pct = abs((cex_price - price) / price * 100)
            spreads.append(round(spread_pct, 6))

    if not spreads:
        return {
            "avg_spread_pct": None,
            "max_spread_pct": None,
            "min_spread_pct": None,
        }

    from statistics import mean
    return {
        "avg_spread_pct": round(mean(spreads), 6),
        "max_spread_pct": max(spreads),
        "min_spread_pct": min(spreads),
    }

def discover_weekly_reports(weekly_dir: Path) -> list[dict]:
    """
    Scan the weekly output directory and return a sorted list of weekly report metadata dicts.
    Each dict contains: path, filename, period_id, iso_year, iso_week, start_date, end_date, report.
    """
    results = []
    for f in weekly_dir.glob("weekly_*.json"):
        try:
            report = json.loads(f.read_text())
            pm = report.get("period_metadata", {})
            iso_year = pm.get("iso_year")
            iso_week = pm.get("iso_week")
            start_date = pm.get("start_date")
            end_date = pm.get("end_date")
            if iso_year is None or iso_week is None:
                print(f"  ⚠ Skipping weekly file missing metadata: {f.name}")
                continue
            results.append({
                "path": f,
                "filename": f.name,
                "period_id": pm.get("period_id"),
                "iso_year": iso_year,
                "iso_week": iso_week,
                "start_date": start_date,
                "end_date": end_date,
                "report": report,
            })
        except Exception as e:
            print(f"  ⚠ Failed to load weekly file {f.name}: {e}")
            continue
    return sorted(results, key=lambda x: (x["iso_year"], x["iso_week"]))

def group_by_calendar_month(weekly_reports: list[dict]) -> dict:
    """
    Group weekly reports by the calendar month where the majority of their days fall.
    A week spanning Mon Jan 27 – Sun Feb 2 has 5 days in January — it belongs to January.
    Returns dict keyed by (year, month) tuples, values are lists of weekly report dicts,
    sorted by (iso_year, iso_week) within each month.
    """
    from collections import defaultdict
    from datetime import date, timedelta

    groups = defaultdict(list)

    for wr in weekly_reports:
        start_str = wr.get("start_date")
        end_str = wr.get("end_date")

        if not start_str or not end_str:
            print(f"  ⚠ Skipping weekly report missing start/end date: {wr['filename']}")
            continue

        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)

        # Count days per calendar month across this week's span
        month_day_counts = defaultdict(int)
        current = start
        while current <= end:
            month_day_counts[(current.year, current.month)] += 1
            current += timedelta(days=1)

        # Assign to the month with the most days
        majority_month = max(month_day_counts, key=month_day_counts.get)
        groups[majority_month].append(wr)

    return dict(sorted(groups.items()))

def aggregate_monthly_price(weekly_reports: list[dict]) -> dict:
    """
    Aggregate price data from weekly reports into a monthly summary.
    Open: from first week's price.open_usd
    Close: from last week's price.close_usd
    High/Low: max/min across all weekly high/low values
    Averages: weighted by days_included where available, otherwise simple mean
    """
    from statistics import mean, mode

    def get_price(wr, field):
        return (wr["report"].get("price") or {}).get(field)

    weekly_data = [wr["report"].get("price") or {} for wr in weekly_reports]

    opens = [w.get("open_usd") for w in weekly_data if w.get("open_usd") is not None]
    closes = [w.get("close_usd") for w in weekly_data if w.get("close_usd") is not None]
    highs = [w.get("high_usd") for w in weekly_data if w.get("high_usd") is not None]
    lows = [w.get("low_usd") for w in weekly_data if w.get("low_usd") is not None]
    avg_vols = [w.get("avg_volume_24h_usd") for w in weekly_data if w.get("avg_volume_24h_usd") is not None]
    total_vols = [w.get("total_volume_week_usd") for w in weekly_data if w.get("total_volume_week_usd") is not None]
    avg_mcaps = [w.get("avg_market_cap_usd") for w in weekly_data if w.get("avg_market_cap_usd") is not None]
    close_mcaps = [w.get("close_market_cap_usd") for w in weekly_data if w.get("close_market_cap_usd") is not None]
    ath_vals = [w.get("ath_usd") for w in weekly_data if w.get("ath_usd") is not None]
    ath_dd_vals = [w.get("ath_drawdown_pct") for w in weekly_data if w.get("ath_drawdown_pct") is not None]
    cex_prices = [w.get("avg_cex_price_usd") for w in weekly_data if w.get("avg_cex_price_usd") is not None]
    up_vols = [w.get("volume_on_up_days_usd") for w in weekly_data if w.get("volume_on_up_days_usd") is not None]
    down_vols = [w.get("volume_on_down_days_usd") for w in weekly_data if w.get("volume_on_down_days_usd") is not None]
    volume_leans = [w.get("volume_lean") for w in weekly_data if w.get("volume_lean") is not None]
    days_list = [w.get("days_included") for w in weekly_data if w.get("days_included") is not None]

    open_usd = opens[0] if opens else None
    close_usd = closes[-1] if closes else None
    price_change_pct = round((close_usd - open_usd) / open_usd * 100, 4) if open_usd and close_usd else None
    high_usd = max(highs) if highs else None
    drawdown_from_high = round((close_usd - high_usd) / high_usd * 100, 4) if close_usd and high_usd else None

    return {
        "open_usd": open_usd,
        "close_usd": close_usd,
        "high_usd": high_usd,
        "low_usd": min(lows) if lows else None,
        "price_change_month_pct": price_change_pct,
        "drawdown_from_period_high_pct": drawdown_from_high,
        "ath_usd": ath_vals[-1] if ath_vals else None,
        "ath_drawdown_pct": ath_dd_vals[-1] if ath_dd_vals else None,
        "avg_volume_24h_usd": round(mean(avg_vols), 2) if avg_vols else None,
        "total_volume_month_usd": round(sum(total_vols), 2) if total_vols else None,
        "avg_market_cap_usd": round(mean(avg_mcaps), 2) if avg_mcaps else None,
        "close_market_cap_usd": close_mcaps[-1] if close_mcaps else None,
        "avg_cex_price_usd": round(mean(cex_prices), 6) if cex_prices else None,
        "volume_on_up_days_usd": round(sum(up_vols), 2) if up_vols else None,
        "volume_on_down_days_usd": round(sum(down_vols), 2) if down_vols else None,
        "volume_lean": mode(volume_leans) if volume_leans else None,
        "weeks_included": len(weekly_reports),
    }

def aggregate_monthly_technicals(weekly_reports: list[dict]) -> dict:
    """
    Aggregate technical indicators from weekly reports into a monthly summary.
    All values taken from the last weekly report (period-end).
    """
    def last_val(field):
        for wr in reversed(weekly_reports):
            tech = (wr["report"].get("technicals") or {})
            v = tech.get(field)
            if v is not None:
                return v
        return None

    def last_nested(outer, inner):
        for wr in reversed(weekly_reports):
            tech = (wr["report"].get("technicals") or {})
            obj = tech.get(outer)
            if obj and obj.get(inner) is not None:
                return obj.get(inner)
        return None

    last_tech = None
    for wr in reversed(weekly_reports):
        t = wr["report"].get("technicals")
        if t is not None:
            last_tech = t
            break

    close_price = None
    for wr in reversed(weekly_reports):
        p = (wr["report"].get("price") or {}).get("close_usd")
        if p is not None:
            close_price = p
            break

    sma_50 = last_val("sma_50_at_close")
    ema_200 = last_val("ema_200_at_close")
    price_vs_sma50_pct = round((close_price - sma_50) / sma_50 * 100, 4) if close_price and sma_50 else None
    price_vs_ema200_pct = round((close_price - ema_200) / ema_200 * 100, 4) if close_price and ema_200 else None

    macd_at_close = None
    for wr in reversed(weekly_reports):
        m = (wr["report"].get("technicals") or {}).get("macd_at_close")
        if m is not None:
            macd_at_close = m
            break

    bb_at_close = None
    for wr in reversed(weekly_reports):
        b = (wr["report"].get("technicals") or {}).get("bollinger_bands_at_close")
        if b is not None:
            bb_at_close = b
            break

    return {
        "rsi_14_at_close": last_val("rsi_14_at_close"),
        "rsi_lean": last_val("rsi_lean"),
        "macd_at_close": macd_at_close,
        "macd_lean": last_val("macd_lean"),
        "bollinger_bands_at_close": bb_at_close,
        "bb_lean": last_val("bb_lean"),
        "sma_50_at_close": sma_50,
        "price_vs_sma50_pct": price_vs_sma50_pct,
        "ema_200_at_close": ema_200,
        "price_vs_ema200_pct": price_vs_ema200_pct,
        "ema200_lean": last_val("ema200_lean"),
    }

def aggregate_monthly_derivatives(weekly_reports: list[dict]) -> dict:
    """
    Aggregate derivatives data from weekly reports into a monthly summary.
    OI: open from first week, close from last week, change computed across period.
    Funding rate: mean of weekly averages across the month.
    BTC L/S ratio: period-end from last weekly report.
    """
    from statistics import mean

    def last_val(field):
        for wr in reversed(weekly_reports):
            d = (wr["report"].get("derivatives") or {})
            v = d.get(field)
            if v is not None:
                return v
        return None

    def first_val(field):
        for wr in weekly_reports:
            d = (wr["report"].get("derivatives") or {})
            v = d.get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("derivatives") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 8) if result else None

    oi_open = first_val("oi_open_usd")
    oi_close = last_val("oi_at_close_usd")
    oi_change_pct = round((oi_close - oi_open) / oi_open * 100, 4) if oi_open and oi_close else None

    return {
        "oi_at_close_usd": oi_close,
        "oi_open_usd": oi_open,
        "oi_change_pct": oi_change_pct,
        "oi_lean": last_val("oi_lean"),
        "funding_rate_daily_avg": avg_vals("funding_rate_daily_avg"),
        "funding_rate_7d_rolling_avg": last_val("funding_rate_7d_rolling_avg"),
        "funding_lean": last_val("funding_lean"),
        "btc_long_short_ratio": last_val("btc_long_short_ratio"),
        "btc_long_account_pct": last_val("btc_long_account_pct"),
        "btc_short_account_pct": last_val("btc_short_account_pct"),
        "btc_ls_ratio_lean": last_val("btc_ls_ratio_lean"),
    }

def aggregate_monthly_on_chain(weekly_reports: list[dict]) -> dict:
    """
    Aggregate on-chain data from weekly reports into a monthly summary.
    TVL: start from first week, end from last week, change computed across period.
    Address and flow data: averages across available weekly values.
    """
    from statistics import mean

    def last_val(field):
        for wr in reversed(weekly_reports):
            v = (wr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                return v
        return None

    def first_val(field):
        for wr in weekly_reports:
            v = (wr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 2) if result else None

    def sum_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(sum(result), 2) if result else None

    tvl_start = first_val("tvl_start_usd")
    tvl_end = last_val("tvl_end_usd")
    tvl_change_pct = round((tvl_end - tvl_start) / tvl_start * 100, 4) if tvl_start and tvl_end else None

    if tvl_change_pct is not None:
        if tvl_change_pct > 2:
            tvl_lean = "bullish"
        elif tvl_change_pct < -2:
            tvl_lean = "bearish"
        else:
            tvl_lean = "neutral"
    else:
        tvl_lean = None

    tvl_direction = None
    if tvl_change_pct is not None:
        if tvl_change_pct > 0:
            tvl_direction = "rising"
        elif tvl_change_pct < 0:
            tvl_direction = "falling"
        else:
            tvl_direction = "flat"

    addr_start = first_val("active_addresses_start")
    addr_end = last_val("active_addresses_end")
    if addr_start and addr_end:
        if addr_end > addr_start * 1.02:
            addr_lean = "bullish"
        elif addr_end < addr_start * 0.98:
            addr_lean = "bearish"
        else:
            addr_lean = "neutral"
    else:
        addr_lean = None

    net_flow = sum_vals("exchange_net_flow_usd")
    if net_flow is not None:
        exchange_flow_lean = "bearish" if net_flow > 0 else "bullish" if net_flow < 0 else "neutral"
    else:
        exchange_flow_lean = None

    return {
        "tvl_start_usd": tvl_start,
        "tvl_end_usd": tvl_end,
        "tvl_change_pct": tvl_change_pct,
        "tvl_direction": tvl_direction,
        "tvl_lean": tvl_lean,
        "active_addresses_avg": avg_vals("active_addresses_avg"),
        "active_addresses_start": addr_start,
        "active_addresses_end": addr_end,
        "active_addresses_lean": addr_lean,
        "exchange_inflow_avg_usd": avg_vals("exchange_inflow_avg_usd"),
        "exchange_outflow_avg_usd": avg_vals("exchange_outflow_avg_usd"),
        "exchange_net_flow_usd": net_flow,
        "exchange_flow_lean": exchange_flow_lean,
    }

def aggregate_monthly_macro(weekly_reports: list[dict]) -> dict:
    """
    Aggregate macro data from weekly reports into a monthly summary.
    BTC return: computed from first week open to last week close.
    Dominance, ETH/BTC, DXY, SPY, VIX: start from first week, end from last week.
    Alpha: token monthly return minus BTC monthly return.
    """
    from statistics import mean

    def last_val(field):
        for wr in reversed(weekly_reports):
            v = (wr["report"].get("macro") or {}).get(field)
            if v is not None:
                return v
        return None

    def first_val(field):
        for wr in weekly_reports:
            v = (wr["report"].get("macro") or {}).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("macro") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 4) if result else None

    def pct_change(start, end):
        if start and end:
            return round((end - start) / start * 100, 4)
        return None

    def direction(start, end):
        if start is None or end is None:
            return None
        if end > start * 1.001:
            return "rising"
        elif end < start * 0.999:
            return "falling"
        return "flat"

    # BTC return across month
    btc_return = None
    btc_returns = []
    for wr in weekly_reports:
        v = (wr["report"].get("macro") or {}).get("btc_period_return_pct")
        if v is not None:
            btc_returns.append(v)
    if btc_returns:
        # Compound weekly returns into monthly return
        from functools import reduce
        compound = reduce(lambda acc, r: acc * (1 + r / 100), btc_returns, 1.0)
        btc_return = round((compound - 1) * 100, 4)

    btc_direction = direction(
        first_val("btc_dominance_start") and None,  # use return direction instead
        None
    )
    if btc_return is not None:
        if btc_return > 0.1:
            btc_direction = "rising"
        elif btc_return < -0.1:
            btc_direction = "falling"
        else:
            btc_direction = "flat"

    # Token monthly return for alpha
    token_return = None
    token_returns = []
    for wr in weekly_reports:
        price = wr["report"].get("price") or {}
        o = price.get("open_usd")
        c = price.get("close_usd")
        if o and c:
            token_returns.append((c - o) / o * 100)
    if token_returns:
        from functools import reduce
        compound = reduce(lambda acc, r: acc * (1 + r / 100), token_returns, 1.0)
        token_return = round((compound - 1) * 100, 4)

    alpha = round(token_return - btc_return, 4) if token_return is not None and btc_return is not None else None

    # Dominance
    dom_start = first_val("btc_dominance_start")
    dom_end = last_val("btc_dominance_end")
    dom_dir = direction(dom_start, dom_end)
    dom_lean = "bearish" if dom_dir == "rising" else "bullish" if dom_dir == "falling" else "neutral" if dom_dir == "flat" else None

    # ETH/BTC
    eth_btc_start = first_val("eth_btc_ratio_start")
    eth_btc_end = last_val("eth_btc_ratio_end")
    eth_btc_lean = "bullish" if eth_btc_end and eth_btc_start and eth_btc_end > eth_btc_start else "bearish" if eth_btc_end and eth_btc_start and eth_btc_end < eth_btc_start else "neutral" if eth_btc_start and eth_btc_end else None

    # DXY
    dxy_start = first_val("dxy_start")
    dxy_end = last_val("dxy_end")
    dxy_change = pct_change(dxy_start, dxy_end)
    dxy_dir = direction(dxy_start, dxy_end)
    dxy_lean = "bearish" if dxy_dir == "rising" else "bullish" if dxy_dir == "falling" else "neutral" if dxy_dir == "flat" else None

    # SPY
    spy_start = first_val("spy_start")
    spy_end = last_val("spy_end")
    spy_change = pct_change(spy_start, spy_end)
    spy_lean = "bullish" if spy_change and spy_change > 0 else "bearish" if spy_change and spy_change < 0 else "neutral" if spy_change == 0 else None

    # VIX
    vix_start = first_val("vix_start")
    vix_end = last_val("vix_end")
    vix_avg = avg_vals("vix_avg")
    if vix_end is not None and vix_avg is not None:
        if vix_end < 20 and (vix_start is None or vix_end <= vix_start):
            vix_lean = "bullish"
        elif vix_end > 25 or (vix_start is not None and vix_end > vix_start):
            vix_lean = "bearish"
        else:
            vix_lean = "neutral"
    else:
        vix_lean = None

    return {
        "btc_period_return_pct": btc_return,
        "btc_direction": btc_direction,
        "token_vs_btc_alpha_pct": alpha,
        "btc_dominance_start": dom_start,
        "btc_dominance_end": dom_end,
        "btc_dominance_direction": dom_dir,
        "btc_dominance_lean": dom_lean,
        "total_crypto_mcap_start_usd": first_val("total_crypto_mcap_start_usd"),
        "total_crypto_mcap_end_usd": last_val("total_crypto_mcap_end_usd"),
        "eth_btc_ratio_start": eth_btc_start,
        "eth_btc_ratio_end": eth_btc_end,
        "eth_btc_lean": eth_btc_lean,
        "dxy_start": dxy_start,
        "dxy_end": dxy_end,
        "dxy_change_pct": dxy_change,
        "dxy_lean": dxy_lean,
        "spy_start": spy_start,
        "spy_end": spy_end,
        "spy_change_pct": spy_change,
        "spy_lean": spy_lean,
        "vix_start": vix_start,
        "vix_end": vix_end,
        "vix_avg": vix_avg,
        "vix_lean": vix_lean,
    }

def aggregate_monthly_sentiment(weekly_reports: list[dict]) -> dict:
    """
    Aggregate sentiment data from weekly reports into a monthly summary.
    Fear & Greed: avg/min/max across all weekly averages, period-end from last week.
    LunarCrush fields: averages across weekly averages, period-end from last week.
    """
    from statistics import mean

    def last_val(field):
        for wr in reversed(weekly_reports):
            v = (wr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 4) if result else None

    def min_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                result.append(v)
        return min(result) if result else None

    def max_vals(field):
        result = []
        for wr in weekly_reports:
            v = (wr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                result.append(v)
        return max(result) if result else None

    fg_at_close = last_val("fear_greed_at_close")
    if fg_at_close is not None:
        if fg_at_close >= 60:
            fg_lean = "bearish"
        elif fg_at_close <= 40:
            fg_lean = "bullish"
        else:
            fg_lean = "neutral"
    else:
        fg_lean = None

    lc_sentiment_close = last_val("lc_sentiment_at_close")
    if lc_sentiment_close is not None:
        if lc_sentiment_close >= 60:
            sentiment_lean = "bullish"
        elif lc_sentiment_close <= 40:
            sentiment_lean = "bearish"
        else:
            sentiment_lean = "neutral"
    else:
        sentiment_lean = None

    galaxy_score_close = last_val("lc_galaxy_score_at_close")
    if galaxy_score_close is not None:
        if galaxy_score_close >= 60:
            galaxy_score_lean = "bullish"
        elif galaxy_score_close <= 40:
            galaxy_score_lean = "bearish"
        else:
            galaxy_score_lean = "neutral"
    else:
        galaxy_score_lean = None

    return {
        "fear_greed_avg": avg_vals("fear_greed_avg"),
        "fear_greed_min": min_vals("fear_greed_min"),
        "fear_greed_max": max_vals("fear_greed_max"),
        "fear_greed_at_close": fg_at_close,
        "fear_greed_label_at_close": last_val("fear_greed_label_at_close"),
        "fear_greed_lean": fg_lean,
        "lc_interactions_avg": avg_vals("lc_interactions_avg"),
        "lc_sentiment_avg": avg_vals("lc_sentiment_avg"),
        "lc_sentiment_at_close": lc_sentiment_close,
        "lc_posts_active_avg": avg_vals("lc_posts_active_avg"),
        "lc_contributors_active_avg": avg_vals("lc_contributors_active_avg"),
        "lc_galaxy_score_avg": avg_vals("lc_galaxy_score_avg"),
        "lc_galaxy_score_at_close": galaxy_score_close,
        "galaxy_score_lean": galaxy_score_lean,
        "lc_alt_rank_at_close": last_val("lc_alt_rank_at_close"),
        "lc_social_dominance_avg": avg_vals("lc_social_dominance_avg"),
        "sentiment_lean": sentiment_lean,
    }

def aggregate_monthly_news(weekly_reports: list[dict]) -> dict:
    """
    Aggregate news headlines from weekly reports into a monthly summary.
    Collects all headlines across all weeks, deduplicates by title,
    sorts by published_at descending, caps at 30 headlines.
    """
    MAX_HEADLINES = 30

    seen_titles = set()
    all_headlines = []

    for wr in weekly_reports:
        news = (wr["report"].get("news") or {})
        items = news.get("headlines") or []
        for item in items:
            title = item.get("title")
            if not title:
                continue
            if title in seen_titles:
                continue
            seen_titles.add(title)
            all_headlines.append(item)

    def sort_key(h):
        ts = h.get("published_at")
        return ts if ts else ""

    all_headlines.sort(key=sort_key, reverse=True)
    all_headlines = all_headlines[:MAX_HEADLINES]

    return {
        "headline_count": len(all_headlines),
        "sources_included": list({h.get("source") for h in all_headlines if h.get("source")}),
        "headlines": all_headlines,
    }

def aggregate_monthly_signal_summary(weekly_reports: list[dict]) -> dict:
    """
    Aggregate signal summary from weekly reports into a monthly summary.
    Period-end signals taken from last weekly report.
    Signal streaks computed across weekly overall_lean values.
    """
    SIGNAL_KEYS = [
        "rsi_lean", "macd_lean", "ema200_lean", "bb_lean", "volume_lean",
        "oi_lean", "funding_lean", "tvl_lean", "exchange_flow_lean",
        "active_addresses_lean", "btc_dominance_lean", "eth_btc_lean",
        "dxy_lean", "spy_lean", "vix_lean", "fear_greed_lean",
        "sentiment_lean", "galaxy_score_lean", "btc_ls_ratio_lean",
    ]

    def last_signal(key):
        for wr in reversed(weekly_reports):
            sig = (wr["report"].get("signal_summary") or {}).get("signals") or {}
            val = sig.get(key)
            if val is not None:
                return val
        return None

    period_end_signals = {k: last_signal(k) for k in SIGNAL_KEYS}

    available = [k for k, v in period_end_signals.items() if v is not None]
    null_signals = [k for k, v in period_end_signals.items() if v is None]
    bullish = sum(1 for v in period_end_signals.values() if v == "bullish")
    bearish = sum(1 for v in period_end_signals.values() if v == "bearish")
    neutral = sum(1 for v in period_end_signals.values() if v == "neutral")

    if len(available) == 0:
        overall_lean = None
    elif bullish > bearish and bullish > neutral:
        overall_lean = "bullish"
    elif bearish > bullish and bearish > neutral:
        overall_lean = "bearish"
    else:
        overall_lean = "neutral"

    # Signal streaks across weekly overall_lean values
    weekly_leans = []
    for wr in weekly_reports:
        lean = (wr["report"].get("signal_summary") or {}).get("overall_lean")
        weekly_leans.append(lean)

    current_streak_lean = None
    current_streak_length = 0
    longest_bullish = 0
    longest_bearish = 0

    if weekly_leans:
        current_streak_lean = weekly_leans[-1]
        for lean in reversed(weekly_leans):
            if lean == current_streak_lean:
                current_streak_length += 1
            else:
                break

        streak = 1
        for i in range(1, len(weekly_leans)):
            if weekly_leans[i] == weekly_leans[i - 1] and weekly_leans[i] is not None:
                streak += 1
            else:
                streak = 1
            if weekly_leans[i] == "bullish":
                longest_bullish = max(longest_bullish, streak)
            elif weekly_leans[i] == "bearish":
                longest_bearish = max(longest_bearish, streak)

        if weekly_leans[0] == "bullish":
            longest_bullish = max(longest_bullish, 1)
        elif weekly_leans[0] == "bearish":
            longest_bearish = max(longest_bearish, 1)

    return {
        "signals_evaluated": len(SIGNAL_KEYS),
        "signals_available": len(available),
        "signals_null": len(null_signals),
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "overall_lean": overall_lean,
        "signals": period_end_signals,
        "signal_streaks": {
            "current_streak_lean": current_streak_lean,
            "current_streak_length": current_streak_length,
            "longest_bullish_streak_in_period": longest_bullish,
            "longest_bearish_streak_in_period": longest_bearish,
        },
    }

def build_weekly_breakdown(weekly_reports: list[dict]) -> list[dict]:
    """
    Build a compact weekly breakdown array for inclusion in monthly reports.
    One entry per constituent weekly report.
    """
    breakdown = []
    for wr in weekly_reports:
        report = wr["report"]
        pm = report.get("period_metadata") or {}
        price = report.get("price") or {}
        ss = report.get("signal_summary") or {}
        breakdown.append({
            "period_id": pm.get("period_id"),
            "start_date": pm.get("start_date"),
            "end_date": pm.get("end_date"),
            "overall_lean": ss.get("overall_lean"),
            "close_usd": price.get("close_usd"),
            "price_change_week_pct": price.get("price_change_week_pct"),
            "signals_available": ss.get("signals_available"),
            "data_quality": pm.get("data_quality"),
        })
    return breakdown


def write_monthly_report(cal_year, cal_month, config, paths, weekly_reports,
                          price_agg, technicals_agg, derivatives_agg, on_chain_agg,
                          macro_agg, sentiment_agg, news_agg, signal_summary_agg,
                          accumulation_metadata, cex_dex_spread, weekly_breakdown):
    from datetime import datetime, timezone
    filename = f"monthly_{cal_year}_M{cal_month:02d}.json"
    output_path = paths["monthly"] / filename

    start_date = weekly_reports[0].get("start_date")
    end_date = weekly_reports[-1].get("end_date")
    weeks_included = len(weekly_reports)

    output = {
        "period_metadata": {
            "type": "monthly",
            "period_id": f"{cal_year}-M{cal_month:02d}",
            "cal_year": cal_year,
            "cal_month": cal_month,
            "start_date": start_date,
            "end_date": end_date,
            "token": config["token_name"],
            "weeks_included": weeks_included,
            "weeks_possible": 4,
            "source": "accumulated",
            "script_version": SCRIPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "data_quality": accumulation_metadata["data_quality"],
        },
        "price": price_agg,
        "technicals": technicals_agg,
        "derivatives": derivatives_agg,
        "on_chain": on_chain_agg,
        "macro": macro_agg,
        "sentiment": sentiment_agg,
        "news": news_agg,
        "signal_summary": signal_summary_agg,
        "weekly_breakdown": weekly_breakdown,
        "data_gaps": [],
        "accumulation_metadata": accumulation_metadata,
        "cex_dex_spread": cex_dex_spread,
    }

    try:
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)
        return output_path
    except Exception as e:
        print(f"  Failed to write {filename}: {e}")
        return None

def aggregate_monthly_accumulation_metadata(weekly_reports: list[dict]) -> dict:
    """
    Compute accumulation_metadata block for a monthly report.
    Constituent periods are weeks.
    """
    weeks_possible = 4
    weeks_available = len(weekly_reports)
    weeks_missing = max(0, weeks_possible - weeks_available)

    if weeks_missing == 0:
        data_quality = "complete"
    elif weeks_available >= MIN_WEEKS_FOR_MONTHLY:
        data_quality = "partial"
    else:
        data_quality = "insufficient"

    return {
        "source": "accumulated",
        "constituent_periods": weeks_possible,
        "constituent_periods_available": weeks_available,
        "constituent_periods_missing": weeks_missing,
        "data_quality": data_quality,
    }

def aggregate_cex_dex_spread_monthly(weekly_reports: list[dict]) -> dict:
    """
    Aggregate CEX/DEX spread from weekly reports into a monthly summary.
    """
    from statistics import mean

    avgs = [w["report"].get("cex_dex_spread", {}).get("avg_spread_pct")
            for w in weekly_reports
            if (w["report"].get("cex_dex_spread") or {}).get("avg_spread_pct") is not None]
    maxes = [w["report"].get("cex_dex_spread", {}).get("max_spread_pct")
             for w in weekly_reports
             if (w["report"].get("cex_dex_spread") or {}).get("max_spread_pct") is not None]
    mins = [w["report"].get("cex_dex_spread", {}).get("min_spread_pct")
            for w in weekly_reports
            if (w["report"].get("cex_dex_spread") or {}).get("min_spread_pct") is not None]

    return {
        "avg_spread_pct": round(mean(avgs), 6) if avgs else None,
        "max_spread_pct": max(maxes) if maxes else None,
        "min_spread_pct": min(mins) if mins else None,
    }

def discover_monthly_reports(monthly_dir: Path) -> list[dict]:
    """
    Scan the monthly output directory and return a sorted list of monthly report metadata dicts.
    Each dict contains: path, filename, period_id, cal_year, cal_month, start_date, end_date, report.
    """
    results = []
    for f in monthly_dir.glob("monthly_*.json"):
        try:
            report = json.loads(f.read_text())
            pm = report.get("period_metadata", {})
            cal_year = pm.get("cal_year")
            cal_month = pm.get("cal_month")
            start_date = pm.get("start_date")
            end_date = pm.get("end_date")
            if cal_year is None or cal_month is None:
                print(f"  ⚠ Skipping monthly file missing metadata: {f.name}")
                continue
            results.append({
                "path": f,
                "filename": f.name,
                "period_id": pm.get("period_id"),
                "cal_year": cal_year,
                "cal_month": cal_month,
                "start_date": start_date,
                "end_date": end_date,
                "report": report,
            })
        except Exception as e:
            print(f"  ⚠ Failed to load monthly file {f.name}: {e}")
            continue
    return sorted(results, key=lambda x: (x["cal_year"], x["cal_month"]))

def group_by_quarter(monthly_reports: list[dict]) -> dict:
    """
    Group monthly reports by calendar quarter.
    Returns dict keyed by (year, quarter) tuples (e.g. (2026, 2)),
    values are lists of monthly report dicts sorted by cal_month ascending.
    """
    from collections import defaultdict

    groups = defaultdict(list)

    for mr in monthly_reports:
        cal_year = mr.get("cal_year")
        cal_month = mr.get("cal_month")
        if cal_year is None or cal_month is None:
            print(f"  ⚠ Skipping monthly report missing year/month: {mr['filename']}")
            continue
        quarter = (cal_month - 1) // 3 + 1
        groups[(cal_year, quarter)].append(mr)

    # Sort months within each quarter
    for key in groups:
        groups[key].sort(key=lambda x: x["cal_month"])

    return dict(sorted(groups.items()))

def aggregate_quarterly_price(monthly_reports: list[dict]) -> dict:
    """
    Aggregate price data from monthly reports into a quarterly summary.
    Open: from first month's price.open_usd
    Close: from last month's price.close_usd
    High/Low: max/min across all monthly high/low values
    Totals: summed across months. Averages: mean across months.
    """
    from statistics import mean, mode

    def get_price(mr, field):
        return (mr["report"].get("price") or {}).get(field)

    monthly_data = [mr["report"].get("price") or {} for mr in monthly_reports]

    opens = [m.get("open_usd") for m in monthly_data if m.get("open_usd") is not None]
    closes = [m.get("close_usd") for m in monthly_data if m.get("close_usd") is not None]
    highs = [m.get("high_usd") for m in monthly_data if m.get("high_usd") is not None]
    lows = [m.get("low_usd") for m in monthly_data if m.get("low_usd") is not None]
    avg_vols = [m.get("avg_volume_24h_usd") for m in monthly_data if m.get("avg_volume_24h_usd") is not None]
    total_vols = [m.get("total_volume_month_usd") for m in monthly_data if m.get("total_volume_month_usd") is not None]
    avg_mcaps = [m.get("avg_market_cap_usd") for m in monthly_data if m.get("avg_market_cap_usd") is not None]
    close_mcaps = [m.get("close_market_cap_usd") for m in monthly_data if m.get("close_market_cap_usd") is not None]
    ath_vals = [m.get("ath_usd") for m in monthly_data if m.get("ath_usd") is not None]
    ath_dd_vals = [m.get("ath_drawdown_pct") for m in monthly_data if m.get("ath_drawdown_pct") is not None]
    cex_prices = [m.get("avg_cex_price_usd") for m in monthly_data if m.get("avg_cex_price_usd") is not None]
    up_vols = [m.get("volume_on_up_days_usd") for m in monthly_data if m.get("volume_on_up_days_usd") is not None]
    down_vols = [m.get("volume_on_down_days_usd") for m in monthly_data if m.get("volume_on_down_days_usd") is not None]
    volume_leans = [m.get("volume_lean") for m in monthly_data if m.get("volume_lean") is not None]

    open_usd = opens[0] if opens else None
    close_usd = closes[-1] if closes else None
    high_usd = max(highs) if highs else None
    price_change_pct = round((close_usd - open_usd) / open_usd * 100, 4) if open_usd and close_usd else None
    drawdown_from_high = round((close_usd - high_usd) / high_usd * 100, 4) if close_usd and high_usd else None

    return {
        "open_usd": open_usd,
        "close_usd": close_usd,
        "high_usd": high_usd,
        "low_usd": min(lows) if lows else None,
        "price_change_quarter_pct": price_change_pct,
        "drawdown_from_period_high_pct": drawdown_from_high,
        "ath_usd": ath_vals[-1] if ath_vals else None,
        "ath_drawdown_pct": ath_dd_vals[-1] if ath_dd_vals else None,
        "avg_volume_24h_usd": round(mean(avg_vols), 2) if avg_vols else None,
        "total_volume_quarter_usd": round(sum(total_vols), 2) if total_vols else None,
        "avg_market_cap_usd": round(mean(avg_mcaps), 2) if avg_mcaps else None,
        "close_market_cap_usd": close_mcaps[-1] if close_mcaps else None,
        "avg_cex_price_usd": round(mean(cex_prices), 6) if cex_prices else None,
        "volume_on_up_days_usd": round(sum(up_vols), 2) if up_vols else None,
        "volume_on_down_days_usd": round(sum(down_vols), 2) if down_vols else None,
        "volume_lean": mode(volume_leans) if volume_leans else None,
        "months_included": len(monthly_reports),
    }

def aggregate_quarterly_technicals(monthly_reports: list[dict]) -> dict:
    """
    Aggregate technical indicators from monthly reports into a quarterly summary.
    All values taken from the last monthly report (period-end).
    """
    def last_val(field):
        for mr in reversed(monthly_reports):
            v = (mr["report"].get("technicals") or {}).get(field)
            if v is not None:
                return v
        return None

    close_price = None
    for mr in reversed(monthly_reports):
        p = (mr["report"].get("price") or {}).get("close_usd")
        if p is not None:
            close_price = p
            break

    sma_50 = last_val("sma_50_at_close")
    ema_200 = last_val("ema_200_at_close")
    price_vs_sma50_pct = round((close_price - sma_50) / sma_50 * 100, 4) if close_price and sma_50 else None
    price_vs_ema200_pct = round((close_price - ema_200) / ema_200 * 100, 4) if close_price and ema_200 else None

    macd_at_close = None
    for mr in reversed(monthly_reports):
        m = (mr["report"].get("technicals") or {}).get("macd_at_close")
        if m is not None:
            macd_at_close = m
            break

    bb_at_close = None
    for mr in reversed(monthly_reports):
        b = (mr["report"].get("technicals") or {}).get("bollinger_bands_at_close")
        if b is not None:
            bb_at_close = b
            break

    return {
        "rsi_14_at_close": last_val("rsi_14_at_close"),
        "rsi_lean": last_val("rsi_lean"),
        "macd_at_close": macd_at_close,
        "macd_lean": last_val("macd_lean"),
        "bollinger_bands_at_close": bb_at_close,
        "bb_lean": last_val("bb_lean"),
        "sma_50_at_close": sma_50,
        "price_vs_sma50_pct": price_vs_sma50_pct,
        "ema_200_at_close": ema_200,
        "price_vs_ema200_pct": price_vs_ema200_pct,
        "ema200_lean": last_val("ema200_lean"),
    }

def aggregate_quarterly_derivatives(monthly_reports: list[dict]) -> dict:
    """
    Aggregate derivatives data from monthly reports into a quarterly summary.
    OI: open from first month, close from last month.
    Funding: mean of monthly averages.
    BTC L/S: period-end from last month.
    """
    from statistics import mean

    def last_val(field):
        for mr in reversed(monthly_reports):
            v = (mr["report"].get("derivatives") or {}).get(field)
            if v is not None:
                return v
        return None

    def first_val(field):
        for mr in monthly_reports:
            v = (mr["report"].get("derivatives") or {}).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for mr in monthly_reports:
            v = (mr["report"].get("derivatives") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 8) if result else None

    oi_open = first_val("oi_open_usd")
    oi_close = last_val("oi_at_close_usd")
    oi_change_pct = round((oi_close - oi_open) / oi_open * 100, 4) if oi_open and oi_close else None

    return {
        "oi_at_close_usd": oi_close,
        "oi_open_usd": oi_open,
        "oi_change_pct": oi_change_pct,
        "oi_lean": last_val("oi_lean"),
        "funding_rate_daily_avg": avg_vals("funding_rate_daily_avg"),
        "funding_rate_7d_rolling_avg": last_val("funding_rate_7d_rolling_avg"),
        "funding_lean": last_val("funding_lean"),
        "btc_long_short_ratio": last_val("btc_long_short_ratio"),
        "btc_long_account_pct": last_val("btc_long_account_pct"),
        "btc_short_account_pct": last_val("btc_short_account_pct"),
        "btc_ls_ratio_lean": last_val("btc_ls_ratio_lean"),
    }

def aggregate_quarterly_on_chain(monthly_reports: list[dict]) -> dict:
    """
    Aggregate on-chain data from monthly reports into a quarterly summary.
    TVL: start from first month, end from last month.
    Address and flow data: averages across monthly values.
    """
    from statistics import mean

    def last_val(field):
        for mr in reversed(monthly_reports):
            v = (mr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                return v
        return None

    def first_val(field):
        for mr in monthly_reports:
            v = (mr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for mr in monthly_reports:
            v = (mr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 2) if result else None

    def sum_vals(field):
        result = []
        for mr in monthly_reports:
            v = (mr["report"].get("on_chain") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(sum(result), 2) if result else None

    tvl_start = first_val("tvl_start_usd")
    tvl_end = last_val("tvl_end_usd")
    tvl_change_pct = round((tvl_end - tvl_start) / tvl_start * 100, 4) if tvl_start and tvl_end else None

    if tvl_change_pct is not None:
        tvl_lean = "bullish" if tvl_change_pct > 2 else "bearish" if tvl_change_pct < -2 else "neutral"
        tvl_direction = "rising" if tvl_change_pct > 0 else "falling" if tvl_change_pct < 0 else "flat"
    else:
        tvl_lean = None
        tvl_direction = None

    addr_start = first_val("active_addresses_start")
    addr_end = last_val("active_addresses_end")
    if addr_start and addr_end:
        addr_lean = "bullish" if addr_end > addr_start * 1.02 else "bearish" if addr_end < addr_start * 0.98 else "neutral"
    else:
        addr_lean = None

    net_flow = sum_vals("exchange_net_flow_usd")
    exchange_flow_lean = "bearish" if net_flow and net_flow > 0 else "bullish" if net_flow and net_flow < 0 else "neutral" if net_flow == 0 else None

    return {
        "tvl_start_usd": tvl_start,
        "tvl_end_usd": tvl_end,
        "tvl_change_pct": tvl_change_pct,
        "tvl_direction": tvl_direction,
        "tvl_lean": tvl_lean,
        "active_addresses_avg": avg_vals("active_addresses_avg"),
        "active_addresses_start": addr_start,
        "active_addresses_end": addr_end,
        "active_addresses_lean": addr_lean,
        "exchange_inflow_avg_usd": avg_vals("exchange_inflow_avg_usd"),
        "exchange_outflow_avg_usd": avg_vals("exchange_outflow_avg_usd"),
        "exchange_net_flow_usd": net_flow,
        "exchange_flow_lean": exchange_flow_lean,
    }


def aggregate_quarterly_macro(monthly_reports: list[dict]) -> dict:
    """
    Aggregate macro data from monthly reports into a quarterly summary.
    BTC and token returns: compounded across monthly returns.
    Dominance, ETH/BTC, DXY, SPY, VIX: start from first month, end from last month.
    """
    from statistics import mean
    from functools import reduce

    if not monthly_reports:
        return {}

    sorted_months = sorted(monthly_reports, key=lambda mr: (mr.get("cal_year", 0), mr.get("cal_month", 0)))

    def macro(mr):
        return (mr["report"].get("macro") or {})

    def last_val(field):
        for mr in reversed(sorted_months):
            v = macro(mr).get(field)
            if v is not None:
                return v
        return None

    def first_val(field):
        for mr in sorted_months:
            v = macro(mr).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        vals = [macro(mr).get(field) for mr in sorted_months]
        vals = [v for v in vals if v is not None]
        return round(mean(vals), 4) if vals else None

    def pct_change(start, end):
        if start is None or end is None or start == 0:
            return None
        return round((end - start) / abs(start) * 100, 2)

    def direction(val):
        if val is None:
            return None
        if val > 0.1:
            return "rising"
        if val < -0.1:
            return "falling"
        return "flat"

    # Compound BTC returns
    btc_returns = [macro(mr).get("btc_period_return_pct") for mr in sorted_months]
    btc_returns = [v for v in btc_returns if v is not None]
    if btc_returns:
        btc_compound = reduce(lambda acc, r: acc * (1 + r / 100), btc_returns, 1.0)
        btc_period_return_pct = round((btc_compound - 1) * 100, 2)
        btc_direction = direction(btc_period_return_pct)
    else:
        btc_period_return_pct = None
        btc_direction = None

    # Compound token returns (vs BTC alpha)
    token_returns = [macro(mr).get("token_vs_btc_alpha_pct") for mr in sorted_months]
    token_returns_clean = [v for v in token_returns if v is not None]
    if token_returns_clean and btc_returns:
        token_compound = reduce(lambda acc, r: acc * (1 + r / 100), token_returns_clean, 1.0)
        token_vs_btc_alpha_pct = round((token_compound - 1) * 100, 2)
    else:
        token_vs_btc_alpha_pct = None

    # BTC dominance
    btc_dom_start = first_val("btc_dominance_start")
    btc_dom_end = last_val("btc_dominance_end")
    btc_dom_change = pct_change(btc_dom_start, btc_dom_end)
    btc_dom_lean_end = last_val("btc_dominance_lean")

    # Total crypto market cap
    mcap_start = first_val("total_crypto_mcap_start_usd")
    mcap_end = last_val("total_crypto_mcap_end_usd")
    mcap_change_pct = pct_change(mcap_start, mcap_end)
    mcap_lean = last_val("total_crypto_mcap_lean")

    # ETH/BTC
    eth_btc_start = first_val("eth_btc_start")
    eth_btc_end = last_val("eth_btc_end")
    eth_btc_change_pct = pct_change(eth_btc_start, eth_btc_end)
    eth_btc_lean = last_val("eth_btc_lean")

    # DXY
    dxy_start = first_val("dxy_start")
    dxy_end = last_val("dxy_end")
    dxy_change_pct = pct_change(dxy_start, dxy_end)
    dxy_lean = last_val("dxy_lean")

    # SPY
    spy_start = first_val("spy_start")
    spy_end = last_val("spy_end")
    spy_change_pct = pct_change(spy_start, spy_end)
    spy_lean = last_val("spy_lean")

    # VIX
    vix_start = first_val("vix_start")
    vix_end = last_val("vix_end")
    vix_change_pct = pct_change(vix_start, vix_end)
    vix_avg = avg_vals("vix_avg")
    vix_lean = last_val("vix_lean")

    return {
        "btc_period_return_pct": btc_period_return_pct,
        "btc_direction": btc_direction,
        "token_vs_btc_alpha_pct": token_vs_btc_alpha_pct,
        "btc_dominance_start": btc_dom_start,
        "btc_dominance_end": btc_dom_end,
        "btc_dominance_change_pct": btc_dom_change,
        "btc_dominance_lean": btc_dom_lean_end,
        "total_crypto_mcap_start_usd": mcap_start,
        "total_crypto_mcap_end_usd": mcap_end,
        "total_crypto_mcap_change_pct": mcap_change_pct,
        "total_crypto_mcap_lean": mcap_lean,
        "eth_btc_start": eth_btc_start,
        "eth_btc_end": eth_btc_end,
        "eth_btc_change_pct": eth_btc_change_pct,
        "eth_btc_lean": eth_btc_lean,
        "dxy_start": dxy_start,
        "dxy_end": dxy_end,
        "dxy_change_pct": dxy_change_pct,
        "dxy_lean": dxy_lean,
        "spy_start": spy_start,
        "spy_end": spy_end,
        "spy_change_pct": spy_change_pct,
        "spy_lean": spy_lean,
        "vix_start": vix_start,
        "vix_end": vix_end,
        "vix_change_pct": vix_change_pct,
        "vix_avg": vix_avg,
        "vix_lean": vix_lean,
    }


def aggregate_quarterly_sentiment(monthly_reports: list[dict]) -> dict:
    """
    Aggregate sentiment data from monthly reports into a quarterly summary.
    Fear & Greed: avg/min/max across monthly averages, period-end from last month.
    LunarCrush: averages across monthly averages, period-end from last month.
    """
    from statistics import mean

    def last_val(field):
        for mr in reversed(monthly_reports):
            v = (mr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                return v
        return None

    def avg_vals(field):
        result = []
        for mr in monthly_reports:
            v = (mr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                result.append(v)
        return round(mean(result), 4) if result else None

    def min_vals(field):
        result = []
        for mr in monthly_reports:
            v = (mr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                result.append(v)
        return min(result) if result else None

    def max_vals(field):
        result = []
        for mr in monthly_reports:
            v = (mr["report"].get("sentiment") or {}).get(field)
            if v is not None:
                result.append(v)
        return max(result) if result else None

    fg_at_close = last_val("fear_greed_at_close")
    if fg_at_close is not None:
        fg_lean = "bearish" if fg_at_close >= 60 else "bullish" if fg_at_close <= 40 else "neutral"
    else:
        fg_lean = None

    lc_sentiment_close = last_val("lc_sentiment_at_close")
    if lc_sentiment_close is not None:
        sentiment_lean = "bullish" if lc_sentiment_close >= 60 else "bearish" if lc_sentiment_close <= 40 else "neutral"
    else:
        sentiment_lean = None

    galaxy_score_close = last_val("lc_galaxy_score_at_close")
    if galaxy_score_close is not None:
        galaxy_score_lean = "bullish" if galaxy_score_close >= 60 else "bearish" if galaxy_score_close <= 40 else "neutral"
    else:
        galaxy_score_lean = None

    return {
        "fear_greed_avg": avg_vals("fear_greed_avg"),
        "fear_greed_min": min_vals("fear_greed_min"),
        "fear_greed_max": max_vals("fear_greed_max"),
        "fear_greed_at_close": fg_at_close,
        "fear_greed_label_at_close": last_val("fear_greed_label_at_close"),
        "fear_greed_lean": fg_lean,
        "lc_interactions_avg": avg_vals("lc_interactions_avg"),
        "lc_sentiment_avg": avg_vals("lc_sentiment_avg"),
        "lc_sentiment_at_close": lc_sentiment_close,
        "lc_posts_active_avg": avg_vals("lc_posts_active_avg"),
        "lc_contributors_active_avg": avg_vals("lc_contributors_active_avg"),
        "lc_galaxy_score_avg": avg_vals("lc_galaxy_score_avg"),
        "lc_galaxy_score_at_close": galaxy_score_close,
        "galaxy_score_lean": galaxy_score_lean,
        "lc_alt_rank_at_close": last_val("lc_alt_rank_at_close"),
        "lc_social_dominance_avg": avg_vals("lc_social_dominance_avg"),
        "sentiment_lean": sentiment_lean,
    }


def aggregate_quarterly_news(monthly_reports: list[dict]) -> dict:
    """
    Aggregate news headlines from monthly reports into a quarterly summary.
    Collects all headlines across all months, deduplicates by title,
    sorts by published_at descending, caps at 40 headlines.
    """
    MAX_HEADLINES = 40

    seen_titles = set()
    all_headlines = []

    for mr in monthly_reports:
        news = (mr["report"].get("news") or {})
        items = news.get("headlines") or []
        for item in items:
            title = item.get("title")
            if not title or title in seen_titles:
                continue
            seen_titles.add(title)
            all_headlines.append(item)

    all_headlines.sort(key=lambda h: h.get("published_at") or "", reverse=True)
    all_headlines = all_headlines[:MAX_HEADLINES]

    return {
        "headline_count": len(all_headlines),
        "sources_included": list({h.get("source") for h in all_headlines if h.get("source")}),
        "headlines": all_headlines,
    }


def aggregate_quarterly_signal_summary(monthly_reports: list[dict]) -> dict:
    """
    Aggregate signal summary from monthly reports into a quarterly summary.
    Period-end signals taken from last monthly report.
    Signal streaks computed across monthly overall_lean values.
    """
    SIGNAL_KEYS = [
        "rsi_lean", "macd_lean", "ema200_lean", "bb_lean", "volume_lean",
        "oi_lean", "funding_lean", "tvl_lean", "exchange_flow_lean",
        "active_addresses_lean", "btc_dominance_lean", "eth_btc_lean",
        "dxy_lean", "spy_lean", "vix_lean", "fear_greed_lean",
        "sentiment_lean", "galaxy_score_lean", "btc_ls_ratio_lean",
    ]

    def last_signal(key):
        for mr in reversed(monthly_reports):
            sig = (mr["report"].get("signal_summary") or {}).get("signals") or {}
            val = sig.get(key)
            if val is not None:
                return val
        return None

    period_end_signals = {k: last_signal(k) for k in SIGNAL_KEYS}
    available = [k for k, v in period_end_signals.items() if v is not None]
    null_signals = [k for k, v in period_end_signals.items() if v is None]
    bullish = sum(1 for v in period_end_signals.values() if v == "bullish")
    bearish = sum(1 for v in period_end_signals.values() if v == "bearish")
    neutral = sum(1 for v in period_end_signals.values() if v == "neutral")

    if len(available) == 0:
        overall_lean = None
    elif bullish > bearish and bullish > neutral:
        overall_lean = "bullish"
    elif bearish > bullish and bearish > neutral:
        overall_lean = "bearish"
    else:
        overall_lean = "neutral"

    monthly_leans = [(mr["report"].get("signal_summary") or {}).get("overall_lean") for mr in monthly_reports]

    current_streak_lean = monthly_leans[-1] if monthly_leans else None
    current_streak_length = 0
    longest_bullish = 0
    longest_bearish = 0

    if monthly_leans:
        for lean in reversed(monthly_leans):
            if lean == current_streak_lean:
                current_streak_length += 1
            else:
                break

        streak = 1
        for i in range(1, len(monthly_leans)):
            if monthly_leans[i] == monthly_leans[i - 1] and monthly_leans[i] is not None:
                streak += 1
            else:
                streak = 1
            if monthly_leans[i] == "bullish":
                longest_bullish = max(longest_bullish, streak)
            elif monthly_leans[i] == "bearish":
                longest_bearish = max(longest_bearish, streak)

        if monthly_leans[0] == "bullish":
            longest_bullish = max(longest_bullish, 1)
        elif monthly_leans[0] == "bearish":
            longest_bearish = max(longest_bearish, 1)

    return {
        "signals_evaluated": len(SIGNAL_KEYS),
        "signals_available": len(available),
        "signals_null": len(null_signals),
        "bullish_count": bullish,
        "bearish_count": bearish,
        "neutral_count": neutral,
        "overall_lean": overall_lean,
        "signals": period_end_signals,
        "signal_streaks": {
            "current_streak_lean": current_streak_lean,
            "current_streak_length": current_streak_length,
            "longest_bullish_streak_in_period": longest_bullish,
            "longest_bearish_streak_in_period": longest_bearish,
        },
    }


def build_monthly_breakdown(monthly_reports: list[dict]) -> list[dict]:
    """
    Build a compact monthly breakdown array for inclusion in quarterly reports.
    One entry per constituent monthly report.
    """
    breakdown = []
    for mr in monthly_reports:
        report = mr["report"]
        pm = report.get("period_metadata") or {}
        price = report.get("price") or {}
        ss = report.get("signal_summary") or {}
        breakdown.append({
            "period_id": pm.get("period_id"),
            "start_date": pm.get("start_date"),
            "end_date": pm.get("end_date"),
            "overall_lean": ss.get("overall_lean"),
            "close_usd": price.get("close_usd"),
            "price_change_month_pct": price.get("price_change_month_pct"),
            "signals_available": ss.get("signals_available"),
            "data_quality": pm.get("data_quality"),
        })
    return breakdown


def aggregate_quarterly_accumulation_metadata(monthly_reports: list[dict]) -> dict:
    """
    Compute accumulation_metadata block for a quarterly report.
    Constituent periods are months.
    """
    months_possible = 3
    months_available = len(monthly_reports)
    months_missing = max(0, months_possible - months_available)

    if months_missing == 0:
        data_quality = "complete"
    elif months_available >= MIN_MONTHS_FOR_QUARTERLY:
        data_quality = "partial"
    else:
        data_quality = "insufficient"

    return {
        "source": "accumulated",
        "constituent_periods": months_possible,
        "constituent_periods_available": months_available,
        "constituent_periods_missing": months_missing,
        "data_quality": data_quality,
    }


def aggregate_cex_dex_spread_quarterly(monthly_reports: list[dict]) -> dict:
    """
    Aggregate CEX/DEX spread from monthly reports into a quarterly summary.
    """
    from statistics import mean

    avgs = [mr["report"].get("cex_dex_spread", {}).get("avg_spread_pct")
            for mr in monthly_reports
            if (mr["report"].get("cex_dex_spread") or {}).get("avg_spread_pct") is not None]
    maxes = [mr["report"].get("cex_dex_spread", {}).get("max_spread_pct")
             for mr in monthly_reports
             if (mr["report"].get("cex_dex_spread") or {}).get("max_spread_pct") is not None]
    mins = [mr["report"].get("cex_dex_spread", {}).get("min_spread_pct")
            for mr in monthly_reports
            if (mr["report"].get("cex_dex_spread") or {}).get("min_spread_pct") is not None]

    return {
        "avg_spread_pct": round(mean(avgs), 6) if avgs else None,
        "max_spread_pct": max(maxes) if maxes else None,
        "min_spread_pct": min(mins) if mins else None,
    }


def write_quarterly_report(cal_year, quarter, config, paths, monthly_reports,
                            price_agg, technicals_agg, derivatives_agg, on_chain_agg,
                            macro_agg, sentiment_agg, news_agg, signal_summary_agg,
                            accumulation_metadata, cex_dex_spread, monthly_breakdown):
    from datetime import datetime, timezone
    filename = f"quarterly_{cal_year}_Q{quarter}.json"
    output_path = paths["quarterly"] / filename

    start_date = monthly_reports[0].get("start_date")
    end_date = monthly_reports[-1].get("end_date")

    output = {
        "period_metadata": {
            "type": "quarterly",
            "period_id": f"{cal_year}-Q{quarter}",
            "cal_year": cal_year,
            "quarter": quarter,
            "start_date": start_date,
            "end_date": end_date,
            "token": config["token_name"],
            "months_included": len(monthly_reports),
            "months_possible": 3,
            "source": "accumulated",
            "script_version": SCRIPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "data_quality": accumulation_metadata["data_quality"],
        },
        "price": price_agg,
        "technicals": technicals_agg,
        "derivatives": derivatives_agg,
        "on_chain": on_chain_agg,
        "macro": macro_agg,
        "sentiment": sentiment_agg,
        "news": news_agg,
        "signal_summary": signal_summary_agg,
        "monthly_breakdown": monthly_breakdown,
        "data_gaps": [],
        "accumulation_metadata": accumulation_metadata,
        "cex_dex_spread": cex_dex_spread,
    }

    try:
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)
        return output_path
    except Exception as e:
        print(f"  Failed to write {filename}: {e}")
        return None


def write_weekly_report(iso_year, iso_week, config, paths, daily_reports, daily_data, price_agg, derivatives_agg, on_chain_agg, macro_agg, sentiment_agg, news_agg, signal_summary_agg, accumulation_metadata, cex_dex_spread):
    from datetime import datetime, timezone
    filename = f"weekly_{iso_year}_W{iso_week:02d}.json"
    output_path = paths["weekly"] / filename

    start_date = daily_reports[0]["date_str"]
    end_date = daily_reports[-1]["date_str"]

    output = {
        "period_metadata": {
            "type": "weekly",
            "period_id": f"{iso_year}-W{iso_week:02d}",
            "iso_year": iso_year,
            "iso_week": iso_week,
            "start_date": start_date,
            "end_date": end_date,
            "token": config["token_name"],
            "days_included": price_agg["days_included"],
            "days_possible": 7,
            "source": "accumulated",
            "script_version": SCRIPT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
            "data_quality": accumulation_metadata["data_quality"]
        },
        "price": price_agg,
        "technicals": aggregate_weekly_technicals(daily_data),
        "derivatives": derivatives_agg,
        "on_chain": on_chain_agg,
        "macro": macro_agg,
        "sentiment": sentiment_agg,
        "news": news_agg,
        "signal_summary": signal_summary_agg,
        "data_gaps": [],
        "accumulation_metadata": accumulation_metadata,
        "cex_dex_spread": cex_dex_spread,
    }

    try:
        with open(output_path, "w") as f:
            json.dump(output, f, indent=2)
        return output_path
    except Exception as e:
        print(f"  Failed to write {filename}: {e}")
        return None


def write_accumulation_index(config: dict, paths: dict) -> Path:
    """
    Write a single accumulation_index.json to the token context root.
    Lists all available weekly, monthly, and quarterly periods with key summary fields.
    Axo reads this index to discover what accumulated periods are available.
    """
    from datetime import datetime, timezone

    def summarise_report(f: Path, period_type: str) -> dict | None:
        try:
            report = json.loads(f.read_text())
            pm = report.get("period_metadata") or {}
            ss = report.get("signal_summary") or {}
            price = report.get("price") or {}
            am = report.get("accumulation_metadata") or {}
            return {
                "period_id": pm.get("period_id"),
                "period_type": period_type,
                "start_date": pm.get("start_date"),
                "end_date": pm.get("end_date"),
                "data_quality": am.get("data_quality"),
                "overall_lean": ss.get("overall_lean"),
                "signals_available": ss.get("signals_available"),
                "close_usd": price.get("close_usd"),
                "price_change_pct": (
                    price.get("price_change_week_pct")
                    or price.get("price_change_month_pct")
                    or price.get("price_change_quarter_pct")
                ),
                "filename": f.name,
            }
        except Exception as e:
            print(f"  ⚠ Failed to index {f.name}: {e}")
            return None

    weekly_entries = []
    for f in sorted(paths["weekly"].glob("weekly_*.json")):
        entry = summarise_report(f, "weekly")
        if entry:
            weekly_entries.append(entry)

    monthly_entries = []
    for f in sorted(paths["monthly"].glob("monthly_*.json")):
        entry = summarise_report(f, "monthly")
        if entry:
            monthly_entries.append(entry)

    quarterly_entries = []
    for f in sorted(paths["quarterly"].glob("quarterly_*.json")):
        entry = summarise_report(f, "quarterly")
        if entry:
            quarterly_entries.append(entry)

    all_entries = weekly_entries + monthly_entries + quarterly_entries

    index = {
        "token": config["token_name"],
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S UTC"),
        "schema_version": SCHEMA_VERSION,
        "total_periods": len(all_entries),
        "weekly_count": len(weekly_entries),
        "monthly_count": len(monthly_entries),
        "quarterly_count": len(quarterly_entries),
        "most_recent_weekly": weekly_entries[-1] if weekly_entries else None,
        "most_recent_monthly": monthly_entries[-1] if monthly_entries else None,
        "most_recent_quarterly": quarterly_entries[-1] if quarterly_entries else None,
        "periods": {
            "weekly": weekly_entries,
            "monthly": monthly_entries,
            "quarterly": quarterly_entries,
        },
    }

    index_path = paths["context_root"] / "accumulation_index.json"
    try:
        with open(index_path, "w") as f:
            json.dump(index, f, indent=2)
        return index_path
    except Exception as e:
        print(f"  Failed to write accumulation_index.json: {e}")
        return None


def prune_accumulated_output(paths: dict) -> dict:
    """
    Prune old accumulated files according to retention thresholds.
    Keeps the most recent RETAIN_WEEKLY weekly files and RETAIN_MONTHLY monthly files.
    Quarterly files are never pruned.
    Returns a dict summarising what was deleted.
    """
    deleted = {"weekly": [], "monthly": []}

    # Prune weekly
    weekly_files = sorted(paths["weekly"].glob("weekly_*.json"))
    if len(weekly_files) > RETAIN_WEEKLY:
        to_delete = weekly_files[:-RETAIN_WEEKLY]
        for f in to_delete:
            try:
                f.unlink()
                deleted["weekly"].append(f.name)
                print(f"  Pruned weekly: {f.name}")
            except Exception as e:
                print(f"  ⚠ Failed to prune {f.name}: {e}")

    # Prune monthly
    monthly_files = sorted(paths["monthly"].glob("monthly_*.json"))
    if len(monthly_files) > RETAIN_MONTHLY:
        to_delete = monthly_files[:-RETAIN_MONTHLY]
        for f in to_delete:
            try:
                f.unlink()
                deleted["monthly"].append(f.name)
                print(f"  Pruned monthly: {f.name}")
            except Exception as e:
                print(f"  ⚠ Failed to prune {f.name}: {e}")

    return deleted


def push_accumulated_to_github(paths: dict, config: dict) -> None:
    """
    Push all accumulated output files to GitHub.
    Non-blocking — missing keys log a warning, never crash the script.
    Follows the same pattern as push_report_to_github in market_report.py.
    """
    token = os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPO")
    branch = os.getenv("GITHUB_BRANCH", "main")

    if not token or not repo:
        print("  ⚠ GitHub push skipped — GITHUB_TOKEN or GITHUB_REPO not set in .env")
        return

    token_lower = config["token_name"].lower()
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json"
    }

    def push_file(local_path: Path, github_path: str) -> bool:
        try:
            content = local_path.read_text(encoding="utf-8")
            encoded = base64.b64encode(content.encode()).decode()
            api_url = f"https://api.github.com/repos/{repo}/contents/{github_path}"

            sha = None
            check = requests.get(api_url, headers=headers, timeout=10)
            if check.status_code == 200:
                sha = check.json().get("sha")

            payload = {
                "message": f"accumulator: update {local_path.name}",
                "content": encoded,
                "branch": branch,
            }
            if sha:
                payload["sha"] = sha

            response = requests.put(api_url, json=payload, headers=headers, timeout=15)
            if response.status_code in (200, 201):
                print(f"  ✓ GitHub push — {github_path}")
                return True
            else:
                print(f"  ✗ GitHub push failed — {github_path} HTTP {response.status_code}: {response.text[:100]}")
                return False
        except Exception as e:
            print(f"  ✗ GitHub push failed — {github_path}: {e}")
            return False

    pushed = 0
    failed = 0

    # Push weekly files
    for f in sorted(paths["weekly"].glob("weekly_*.json")):
        github_path = f"context/{token_lower}/accumulated/weekly/{f.name}"
        if push_file(f, github_path):
            pushed += 1
        else:
            failed += 1

    # Push monthly files
    for f in sorted(paths["monthly"].glob("monthly_*.json")):
        github_path = f"context/{token_lower}/accumulated/monthly/{f.name}"
        if push_file(f, github_path):
            pushed += 1
        else:
            failed += 1

    # Push quarterly files
    for f in sorted(paths["quarterly"].glob("quarterly_*.json")):
        github_path = f"context/{token_lower}/accumulated/quarterly/{f.name}"
        if push_file(f, github_path):
            pushed += 1
        else:
            failed += 1

    # Push accumulation index
    index_path = paths["context_root"] / "accumulation_index.json"
    if index_path.exists():
        github_path = f"context/{token_lower}/accumulation_index.json"
        if push_file(index_path, github_path):
            pushed += 1
        else:
            failed += 1

    print(f"  GitHub: {pushed} pushed, {failed} failed.")


def clear_accumulated_output(paths: dict) -> None:
    """
    In --rebuild mode: delete all files in weekly/, monthly/, quarterly/
    and the accumulation_index.json. Does not touch reports/ or archive/.
    """
    for subdir in ["weekly", "monthly", "quarterly"]:
        for f in paths[subdir].glob("*.json"):
            f.unlink()
        for f in paths[subdir].glob("*.md"):
            f.unlink()
    
    index_path = paths["accumulated_base"] / "accumulation_index.json"
    if index_path.exists():
        index_path.unlink()
    
    print("  ✓ Cleared accumulated output — rebuilding from scratch")

def main():
    args = parse_args()
    
    print(f"\nMuneo Market Accumulator v{SCRIPT_VERSION}")
    print(f"Config: {args.config}")
    if args.rebuild:
        print("Mode: REBUILD")
    print()
    
    # Load config
    config = load_config(args.config)
    print(f"Token: {config['token_name']}")
    print(f"Output prefix: {config['output_prefix']}")
    
    # Setup directories
    paths = setup_directories(config)
    
    # Rebuild mode: clear existing accumulated output
    if args.rebuild:
        clear_accumulated_output(paths)
    
    # Discover daily reports
    print(f"\nScanning {paths['reports']} for {config['output_prefix']}*.json ...")
    daily_reports = discover_daily_reports(paths["reports"], config["output_prefix"])
    print(f"  Found {len(daily_reports)} daily reports")
    
    if not daily_reports:
        print("  No daily reports found. Nothing to accumulate.")
        return
    
    # Group by ISO week
    weekly_groups = group_by_iso_week(daily_reports)
    print(f"  Grouped into {len(weekly_groups)} ISO weeks:")
    for (yr, wk), reports in sorted(weekly_groups.items()):
        days = len(reports)
        status = "✓ eligible" if days >= MIN_DAYS_FOR_WEEKLY else f"✗ only {days} days (need {MIN_DAYS_FOR_WEEKLY})"
        print(f"    {yr}-W{wk:02d}: {days} days — {status}")

    print("\nGenerating weekly reports...")
    weekly_written = []
    for (yr, wk), reports in sorted(weekly_groups.items()):
        deduped = deduplicate_by_date(reports)
        if len(deduped) < MIN_DAYS_FOR_WEEKLY:
            print(f"  Skipping {yr}-W{wk:02d}: only {len(deduped)} days after dedup")
            continue
        daily_data = [d for d in [load_daily_report(r["path"]) for r in deduped] if d is not None]
        if len(daily_data) < MIN_DAYS_FOR_WEEKLY:
            print(f"  Skipping {yr}-W{wk:02d}: only {len(daily_data)} reports loaded")
            continue
        price_agg = aggregate_weekly_price(daily_data)
        derivatives_agg = aggregate_weekly_derivatives(daily_data)
        on_chain_agg = aggregate_weekly_on_chain(daily_data)
        macro_agg = aggregate_weekly_macro(daily_data)
        sentiment_agg = aggregate_weekly_sentiment(daily_data)
        news_agg = aggregate_weekly_news(daily_data)
        signal_summary_agg = aggregate_weekly_signal_summary(daily_data)
        accumulation_metadata = aggregate_accumulation_metadata(deduped, daily_data)
        cex_dex_spread = aggregate_cex_dex_spread(daily_data)
        output_path = write_weekly_report(yr, wk, config, paths, deduped, daily_data, price_agg, derivatives_agg, on_chain_agg, macro_agg, sentiment_agg, news_agg, signal_summary_agg, accumulation_metadata, cex_dex_spread)
        if output_path:
            weekly_written.append(output_path)
            print(f"  Written: {output_path.name}")
    print(f"\n{len(weekly_written)} weekly reports written.")

    # Monthly aggregation
    print("\nGenerating monthly reports...")
    weekly_dir = paths["weekly"]
    all_weekly = discover_weekly_reports(weekly_dir)
    monthly_groups = group_by_calendar_month(all_weekly)
    print(f"  Found {len(monthly_groups)} calendar months from {len(all_weekly)} weekly reports")

    monthly_written = []
    for (yr, mo), weeks in sorted(monthly_groups.items()):
        if len(weeks) < MIN_WEEKS_FOR_MONTHLY:
            print(f"  Skipping {yr}-M{mo:02d}: only {len(weeks)} weeks (need {MIN_WEEKS_FOR_MONTHLY})")
            continue
        price_agg = aggregate_monthly_price(weeks)
        technicals_agg = aggregate_monthly_technicals(weeks)
        derivatives_agg = aggregate_monthly_derivatives(weeks)
        on_chain_agg = aggregate_monthly_on_chain(weeks)
        macro_agg = aggregate_monthly_macro(weeks)
        sentiment_agg = aggregate_monthly_sentiment(weeks)
        news_agg = aggregate_monthly_news(weeks)
        signal_summary_agg = aggregate_monthly_signal_summary(weeks)
        accumulation_metadata = aggregate_monthly_accumulation_metadata(weeks)
        cex_dex_spread_agg = aggregate_cex_dex_spread_monthly(weeks)
        weekly_breakdown = build_weekly_breakdown(weeks)
        output_path = write_monthly_report(
            yr, mo, config, paths, weeks,
            price_agg, technicals_agg, derivatives_agg, on_chain_agg,
            macro_agg, sentiment_agg, news_agg, signal_summary_agg,
            accumulation_metadata, cex_dex_spread_agg, weekly_breakdown
        )
        if output_path:
            monthly_written.append(output_path)
            print(f"  Written: {output_path.name}")
    print(f"\n{len(monthly_written)} monthly reports written.")

    # Quarterly aggregation
    print("\nGenerating quarterly reports...")
    monthly_dir = paths["monthly"]
    all_monthly = discover_monthly_reports(monthly_dir)
    quarterly_groups = group_by_quarter(all_monthly)
    print(f"  Found {len(quarterly_groups)} quarters from {len(all_monthly)} monthly reports")

    quarterly_written = []
    for (yr, q), months in sorted(quarterly_groups.items()):
        if len(months) < MIN_MONTHS_FOR_QUARTERLY:
            print(f"  Skipping {yr}-Q{q}: only {len(months)} months (need {MIN_MONTHS_FOR_QUARTERLY})")
            continue
        price_agg = aggregate_quarterly_price(months)
        technicals_agg = aggregate_quarterly_technicals(months)
        derivatives_agg = aggregate_quarterly_derivatives(months)
        on_chain_agg = aggregate_quarterly_on_chain(months)
        macro_agg = aggregate_quarterly_macro(months)
        sentiment_agg = aggregate_quarterly_sentiment(months)
        news_agg = aggregate_quarterly_news(months)
        signal_summary_agg = aggregate_quarterly_signal_summary(months)
        accumulation_metadata = aggregate_quarterly_accumulation_metadata(months)
        cex_dex_spread_agg = aggregate_cex_dex_spread_quarterly(months)
        monthly_breakdown = build_monthly_breakdown(months)
        output_path = write_quarterly_report(
            yr, q, config, paths, months,
            price_agg, technicals_agg, derivatives_agg, on_chain_agg,
            macro_agg, sentiment_agg, news_agg, signal_summary_agg,
            accumulation_metadata, cex_dex_spread_agg, monthly_breakdown
        )
        if output_path:
            quarterly_written.append(output_path)
            print(f"  Written: {output_path.name}")
    print(f"\n{len(quarterly_written)} quarterly reports written.")

    # Prune old accumulated files
    print("\nPruning old accumulated files...")
    pruned = prune_accumulated_output(paths)
    weekly_pruned = len(pruned["weekly"])
    monthly_pruned = len(pruned["monthly"])
    if weekly_pruned == 0 and monthly_pruned == 0:
        print("  Nothing to prune.")
    else:
        print(f"  Pruned {weekly_pruned} weekly, {monthly_pruned} monthly files.")

    # Write accumulation index
    print("\nWriting accumulation index...")
    index_path = write_accumulation_index(config, paths)
    if index_path:
        print(f"  Written: {index_path.name}")
    else:
        print("  WARNING: accumulation index failed to write")

    # Push accumulated output to GitHub
    print("\nPushing accumulated output to GitHub...")
    push_accumulated_to_github(paths, config)

    print("Accumulator run complete.")

if __name__ == "__main__":
    main()